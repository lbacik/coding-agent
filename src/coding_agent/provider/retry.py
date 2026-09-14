from __future__ import annotations

from typing import Protocol

from langchain_core.exceptions import ModelError
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage

DEFAULT_MAX_ATTEMPTS = 3


class InvokableModel(Protocol):
    def invoke(self, input: LanguageModelInput) -> BaseMessage: ...


def invoke_with_retry(
    model: InvokableModel, input: LanguageModelInput, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> BaseMessage:
    """Retry a transient provider failure against the same Pinned Model — never
    a different one (`L1-5`).

    There is no fallback wiring anywhere in this adapter, so "the retry hits
    the same Pinned Model" holds by construction: this function takes exactly
    one `model` and only ever calls that one. A real 529/503 cannot be
    summoned from a provider on demand, so this is exercised in tests against
    a fake raising `ModelError` — langchain's own provider-neutral taxonomy,
    keyed to `is_retryable` rather than to any one provider's status codes.
    """
    last_exc: ModelError | None = None
    for _ in range(max_attempts):
        try:
            return model.invoke(input)
        except ModelError as exc:
            if not exc.is_retryable:
                raise
            last_exc = exc
    assert last_exc is not None
    raise last_exc
