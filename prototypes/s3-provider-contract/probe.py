"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

Answers issue #24: do both providers pass the v1 runtime contract through
`init_chat_model`, and what can a preflight capability assertion actually
assert? Six probes over the two pins in `providers.py`, at L1's own definition
of the layer — real provider API, smallest possible prompts.

Everything is recorded rather than asserted. A probe that "fails" is a
finding, not a broken test: the point is to come back with `L1-1…6` either
confirmed as assertable exactly as written or named with the amendment they
need, and a guess dressed as a pass would defeat that.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from providers import EFFORT, PINS, Pin

HERE = Path(__file__).parent
TRANSCRIPTS = HERE / "transcripts"

RECORD: dict[str, Any] = {}


def record(pin: str, probe: str, payload: dict[str, Any]) -> None:
    RECORD.setdefault(pin, {})[probe] = payload
    print(f"  [{pin}/{probe}] " + json.dumps(payload, default=str)[:600], flush=True)


def caught(exc: BaseException) -> dict[str, Any]:
    """Everything our own classifier would have to see, and nothing we invent."""
    out: dict[str, Any] = {
        "exception_class": f"{type(exc).__module__}.{type(exc).__name__}",
        "str": str(exc)[:600],
        "status_code": getattr(exc, "status_code", None),
    }
    response = getattr(exc, "response", None)
    if response is not None:
        out["response_status"] = getattr(response, "status_code", None)
        headers = dict(getattr(response, "headers", {}) or {})
        # The §6 rate-limit table classifies on header *values*, so keep the
        # ones it names and the retry hint, never the whole header bag.
        out["headers_of_interest"] = {
            k: v
            for k, v in headers.items()
            if k.lower().startswith(("x-ratelimit", "retry-after", "anthropic-ratelimit"))
        }
    body = getattr(exc, "body", None)
    if body is not None:
        out["body"] = body[:2000] if isinstance(body, str) else body
    return out


def usage_of(message: AIMessage) -> dict[str, Any]:
    return {
        "usage_metadata": message.usage_metadata,
        "model_name": message.response_metadata.get("model_name")
        or message.response_metadata.get("model"),
        "response_metadata_keys": sorted(message.response_metadata.keys()),
    }


# --------------------------------------------------------------------------
# The implementer's toolset, in the shape S3 will bind it. Bodies are stubs:
# L1 asks whether the *call* arrives well-formed, not whether the tool works.
# --------------------------------------------------------------------------


@tool
def read_file(path: str) -> str:
    """Read a file from the repository being worked on. Path is repo-relative."""
    return f"<contents of {path}>"


@tool
def write_file(path: str, content: str) -> str:
    """Write a file in the repository being worked on. Path is repo-relative."""
    return f"wrote {len(content)} bytes to {path}"


@tool
def run_tests(command: str, paths: list[str], env: dict[str, str]) -> str:
    """Run the project's tests.

    `paths` is the list of test files to run, `env` extra environment variables.
    Deliberately awkward: a list and a mapping in one call is where a provider
    that flattens arguments to strings shows itself.
    """
    return f"ran {command} over {paths} with {env}"


@tool
def read_skill_resource(skill: str, path: str) -> str:
    """Read a companion file belonging to an activated skill."""
    return f"<{skill}/{path}>"


TOOLSET = [read_file, write_file, run_tests, read_skill_resource]


# --------------------------------------------------------------------------
# P0 — does the effort knob reach the provider at all?
#
# A pin that is silently dropped is worse than one that is refused: the run
# looks correct and is not the run the Attempt recorded. Sending a value the
# provider must reject proves the channel without paying for a completion.
# --------------------------------------------------------------------------


def probe_param_channel(pin: Pin) -> None:
    nonsense = "medium-but-spelled-wrong"
    overrides = (
        {"output_config": {"effort": nonsense}}
        if pin.name == "anthropic"
        else {"reasoning_effort": nonsense}
    )
    try:
        pin.build(**overrides).invoke("hi")
    except Exception as exc:
        seen = caught(exc)
        record(
            pin.name,
            "param_channel",
            {
                "verdict": "reaches the provider",
                "sent": overrides,
                "refused_with": seen,
            },
        )
        return
    record(
        pin.name,
        "param_channel",
        {
            "verdict": "SILENTLY ACCEPTED — the effort pin may never reach the provider",
            "sent": overrides,
        },
    )


# --------------------------------------------------------------------------
# P1 — L1-1. Bind the toolset, ask for a call, and complete the round-trip.
#
# The round-trip is the half that matters. Getting a well-formed `tool_calls`
# entry says the provider can speak; feeding it back and having the provider
# accept the history is what says one tool loop serves both. A provider that
# needs its own reasoning blocks echoed fails here and nowhere earlier.
# --------------------------------------------------------------------------


def probe_tool_call(pin: Pin) -> None:
    llm = pin.build().bind_tools(TOOLSET)
    ask = (
        "Run the project's tests over exactly two files, tests/test_a.py and "
        "tests/test_b.py, with CI set to 1 and COLOR set to 0. Use the tools; "
        "do not answer in prose."
    )
    history: list[Any] = [HumanMessage(ask)]
    try:
        first = llm.invoke(history)
    except Exception as exc:
        record(pin.name, "tool_call", {"verdict": "no call", "error": caught(exc)})
        return

    calls = first.tool_calls
    shape = {
        "n_calls": len(calls),
        "calls": [
            {
                "name": c["name"],
                "args": c["args"],
                "args_python_types": {k: type(v).__name__ for k, v in c["args"].items()},
                "has_id": bool(c.get("id")),
                "type": c.get("type"),
            }
            for c in calls
        ],
        "invalid_tool_calls": first.invalid_tool_calls,
        # What the loop would have to carry back besides text and tool_calls.
        "content_block_types": (
            [b.get("type") for b in first.content if isinstance(b, dict)]
            if isinstance(first.content, list)
            else f"plain {type(first.content).__name__}"
        ),
        **usage_of(first),
    }

    if not calls:
        record(pin.name, "tool_call", {"verdict": "NO TOOL CALL — asked and did not call", **shape})
        return

    # Feed the result back exactly as our own loop would, unmodified message
    # object included, and see whether the provider accepts its own history.
    history.append(first)
    for c in calls:
        history.append(ToolMessage(content=f"ran {c['name']}: 2 passed", tool_call_id=c["id"]))
    try:
        second = llm.invoke(history)
        shape["round_trip"] = {
            "verdict": "accepted",
            "text": (second.text if isinstance(second.text, str) else second.text())[:300],
            "further_calls": len(second.tool_calls),
            **usage_of(second),
        }
    except Exception as exc:
        shape["round_trip"] = {
            "verdict": "REFUSED its own history — one tool loop does NOT serve both as written",
            "error": caught(exc),
        }
    record(pin.name, "tool_call", {"verdict": "called", **shape})


# --------------------------------------------------------------------------
# P2 — L1-2, the observable half.
#
# The row asks for structured output "for the classifier role". Whether that
# role exists in the contract at all is a design question this probe cannot
# settle; what it can settle is whether the mechanism works if a node ever
# needs it. The schema below is deliberately a *contract* classification —
# §6's rate-limit table — because that is the closest thing the contract has
# to a classifier, and it is deterministic there.
# --------------------------------------------------------------------------


class FailureClassification(BaseModel):
    """The shape a model-driven classifier would have to return."""

    classification: str = Field(description="one of: credential-failure, permission-refusal, primary-rate-limit, secondary-rate-limit")
    transient: bool = Field(description="true if the Automatic Retry Budget should be spent on it")
    evidence: list[str] = Field(description="the status, header values and error signals the call was made on")


def probe_structured_output(pin: Pin) -> None:
    observation = (
        "A GitHub write returned HTTP 403 with header x-ratelimit-remaining: 0 "
        "and no Retry-After header. Classify it."
    )
    for method in ("json_schema", "function_calling"):
        try:
            llm = pin.build().with_structured_output(FailureClassification, method=method, include_raw=True)
            out = llm.invoke(observation)
            parsed = out["parsed"]
            record(
                pin.name,
                f"structured_output[{method}]",
                {
                    "verdict": "parsed" if parsed is not None else "RETURNED NOTHING PARSEABLE",
                    "parsed": parsed.model_dump() if parsed is not None else None,
                    "parsing_error": str(out.get("parsing_error"))[:300],
                    **usage_of(out["raw"]),
                },
            )
        except Exception as exc:
            record(pin.name, f"structured_output[{method}]", {"verdict": "unavailable", "error": caught(exc)})


# --------------------------------------------------------------------------
# P3 — L1-3. Tokens present and non-zero; cost derivable.
#
# "Cost derivable" is the half worth probing. `usage_metadata` reports tokens,
# never money, and the buckets are not priced alike: a cache read is a
# fraction of a fresh input token and a reasoning token bills as output. So
# the probe sends the same long prefix twice and records which buckets move.
# --------------------------------------------------------------------------


def probe_usage_and_cost(pin: Pin) -> None:
    # Long enough to clear both providers' minimum cacheable prefix.
    prefix = "The Attempt Header is never compacted or summarised. " * 400
    ask = [HumanMessage(f"{prefix}\n\nReply with the single word: ok")]
    kwargs: dict[str, Any] = (
        {"cache_control": {"type": "ephemeral"}} if pin.name == "anthropic" else {}
    )
    rounds = []
    for i in (1, 2):
        try:
            message = pin.build(**kwargs).invoke(ask)
            rounds.append({"round": i, **usage_of(message)})
        except Exception as exc:
            rounds.append({"round": i, "error": caught(exc)})
        time.sleep(1)
    record(pin.name, "usage_and_cost", {"rounds": rounds, "cache_control_sent": kwargs or None})


# --------------------------------------------------------------------------
# P4 — L1-5, and the detection half of L1-4.
#
# A genuine "overloaded" cannot be summoned on demand, so the probe records
# the errors it *can* summon and asks a narrower, answerable question: is the
# surface our classifier gets to see rich enough to tell overloaded from a
# rate limit at all? Anything beyond that is a unit-level rule over a
# synthetic response, exactly as #22 forced on L3-IMP-2 and L3-IMP-3.
# --------------------------------------------------------------------------


def probe_error_surface(pin: Pin) -> None:
    provider_env = "ANTHROPIC_API_KEY" if pin.name == "anthropic" else "OPENAI_API_KEY"
    cases: dict[str, Any] = {}

    # A model the account cannot reach: the nearest summonable neighbour of a
    # capability refusal, and the one L1-4 has to catch before an Attempt.
    try:
        pin.build(model=f"{pin.name}:definitely-not-a-real-model-{int(time.time())}").invoke("hi")
        cases["unknown_model"] = {"verdict": "NO ERROR"}
    except Exception as exc:
        cases["unknown_model"] = caught(exc)

    # A broken credential: §6 classifies 401 as Credential Failure and halts
    # the Worker, so the surface it is classified on had better be clear.
    real = os.environ.get(provider_env, "")
    os.environ[provider_env] = "sk-not-a-real-key-000"
    try:
        pin.build().invoke("hi")
        cases["bad_credential"] = {"verdict": "NO ERROR"}
    except Exception as exc:
        cases["bad_credential"] = caught(exc)
    finally:
        os.environ[provider_env] = real

    # An invalid request, to see whether a 400 is distinguishable from the two
    # above by anything better than its message text.
    try:
        pin.build(max_tokens=-1).invoke("hi")
        cases["invalid_request"] = {"verdict": "NO ERROR"}
    except Exception as exc:
        cases["invalid_request"] = caught(exc)

    cases["overloaded"] = {
        "verdict": "NOT SUMMONABLE",
        "note": "529 (anthropic) / 503 (openai) cannot be provoked on demand. "
        "L1-5 is therefore not assertable as a run; the classification is a "
        "unit-level rule over a synthetic response.",
    }
    record(pin.name, "error_surface", cases)


# --------------------------------------------------------------------------
# P5 — L1-4, the observable half. What is knowable before an Attempt opens?
# --------------------------------------------------------------------------


def _get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code, "body": exc.read().decode()[:400]}


def probe_capability_surface(pin: Pin) -> None:
    headers = (
        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}
        if pin.name == "anthropic"
        else {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    )
    declared = _get_json(pin.models_endpoint, headers)

    # And the other route to the same question — §10's own precedent, that
    # capability is established by attempting rather than by reading a grant.
    # Both directions are recorded, because they disagree: a model whose
    # deficiency is only in the *knobs* passes the attempt that omits them.
    attempted: dict[str, list[dict[str, Any]]] = {"runs": []}
    for label, overrides in (
        ("with the pin's own effort knobs", {}),
        ("with the effort knobs removed", _stripped(pin)),
    ):
        run: dict[str, Any] = {"how": label, "model": pin.deficient_model}
        started = time.monotonic()
        try:
            message = (
                pin.build(model=pin.deficient_model, **overrides)
                .bind_tools(TOOLSET)
                .invoke([HumanMessage("Read the file README.md using the tools.")])
            )
            run |= {
                "verdict": "accepted the binding",
                "n_calls": len(message.tool_calls),
                **usage_of(message),
            }
        except Exception as exc:
            run |= {"verdict": "refused", "error": caught(exc)}
        run["seconds"] = round(time.monotonic() - started, 2)
        attempted["runs"].append(run)

    record(
        pin.name,
        "capability_surface",
        {"declared_by_provider": declared, "established_by_attempting": attempted},
    )


def _stripped(pin: Pin) -> dict[str, Any]:
    """The pin's effort/thinking knobs removed — a deficient model rejects them,
    and that rejection would mask the capability question being asked."""
    return {k: None for k in ("thinking", "output_config", "reasoning_effort") if k in pin.kwargs}


PROBES = {
    "param_channel": probe_param_channel,
    "tool_call": probe_tool_call,
    "structured_output": probe_structured_output,
    "usage_and_cost": probe_usage_and_cost,
    "error_surface": probe_error_surface,
    "capability_surface": probe_capability_surface,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", choices=sorted(PROBES), default=sorted(PROBES))
    parser.add_argument("--provider", nargs="*", choices=[p.name for p in PINS],
                        default=[p.name for p in PINS])
    args = parser.parse_args()

    for pin in [p for p in PINS if p.name in args.provider]:
        print(f"\n=== {pin.name}: {pin.model} (effort={EFFORT}) ===", flush=True)
        for name in args.only:
            try:
                PROBES[name](pin)
            except Exception as exc:  # a broken probe must not lose the others
                record(pin.name, name, {"verdict": "PROBE ITSELF BLEW UP", "error": caught(exc)})

    TRANSCRIPTS.mkdir(exist_ok=True)
    out = TRANSCRIPTS / "probe.json"
    out.write_text(json.dumps(RECORD, indent=2, default=str) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
