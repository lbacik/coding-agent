"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

`L1-6`: one small real implementation task per provider, completed through our
own bounded tool loop, with the pinned model recorded beside the Fingerprint.

Deliberately *not* against the sandbox repository. L1 is defined as "real
provider API, smallest possible prompts"; putting GitHub in the loop would
make a failure ambiguous between the provider and the platform, and the real
`issue → pull request` round trip is L4's row, not this one. So the task runs
over a scratch git repository created here, and the verdict comes from
pytest's exit code rather than from the model's account of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from providers import EFFORT, PINS, Pin

HERE = Path(__file__).parent
TRANSCRIPTS = HERE / "transcripts"
MAX_ITERATIONS = 12
TOOL_RESULT_CAP = 20_000  # mirrors the per-tool-result cap L3-IMP-5 will assert

WORKSPACE = Path()  # set per provider in main()

# The task: a failing test that names its own seam, so the run needs no seam
# negotiation (ADR 0008 puts that outside the implementer's conversation).
FAILING_TEST = '''\
from slugify import slugify


def test_lowercases_and_hyphenates():
    assert slugify("Hello World") == "hello-world"


def test_collapses_runs_of_separators():
    assert slugify("  Hello   --  World  ") == "hello-world"


def test_drops_characters_that_are_not_letters_digits_or_separators():
    assert slugify("Hello, World! (v2)") == "hello-world-v2"
'''

TASK = """\
The repository has a test file `tests/test_slugify.py` that imports `slugify`
from a module `slugify` which does not exist yet. Make the tests pass.

Write the implementation in `slugify.py` at the repository root. Do not edit
the tests. Run the tests to check your work before you finish.
"""


def _cap(text: str) -> str:
    if len(text) <= TOOL_RESULT_CAP:
        return text
    return text[:TOOL_RESULT_CAP] + f"\n\n[TRUNCATED at {TOOL_RESULT_CAP} of {len(text)} characters]"


def _inside(path: str) -> Path | None:
    target = (WORKSPACE / path).resolve()
    return target if target.is_relative_to(WORKSPACE.resolve()) else None


@tool
def read_file(path: str) -> str:
    """Read a file from the repository. Path is repo-relative."""
    target = _inside(path)
    if target is None or not target.is_file():
        return f"no such file: {path}"
    return _cap(target.read_text())


@tool
def write_file(path: str, content: str) -> str:
    """Write a file in the repository, creating it if needed. Path is repo-relative."""
    target = _inside(path)
    if target is None:
        return f"refused: {path} is outside the repository"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} characters to {path}"


@tool
def list_files() -> str:
    """List the repository's files."""
    return "\n".join(
        sorted(
            str(p.relative_to(WORKSPACE))
            for p in WORKSPACE.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
    )


TEST_RUNS: list[dict[str, Any]] = []


@tool
def run_tests() -> str:
    """Run the repository's test suite and return its output."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
    )
    TEST_RUNS.append({"exit_code": done.returncode, "tail": (done.stdout or done.stderr)[-400:]})
    return _cap(f"[exit code {done.returncode}]\n{done.stdout}\n{done.stderr}")


TOOLSET = [read_file, write_file, list_files, run_tests]
BY_NAME = {t.name: t for t in TOOLSET}


def _fingerprint(base_sha: str) -> str:
    """Stands in for the real Fingerprint: whatever pins the Attempt to a base."""
    return hashlib.sha256(f"slugify@{base_sha}".encode()).hexdigest()[:16]


def _prepare(scratch: Path) -> str:
    global WORKSPACE
    WORKSPACE = scratch
    if scratch.exists():
        subprocess.run(["rm", "-rf", str(scratch)], check=True)
    (scratch / "tests").mkdir(parents=True)
    (scratch / "tests" / "test_slugify.py").write_text(FAILING_TEST)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=p@p", "-c", "user.name=p",
                                                 "commit", "-qm", "base: a failing test"]):
        subprocess.run(["git", "-C", str(scratch), *args], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(scratch), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def run(pin: Pin, scratch: Path) -> dict[str, Any]:
    """One Attempt-shaped run: bounded loop, usage counted after every response."""
    base_sha = _prepare(scratch / pin.name)
    TEST_RUNS.clear()

    attempt_id = f"L1-6/{pin.name}/{uuid.uuid4().hex[:8]}"
    ledger: dict[str, Any] = {
        # The row L1-6 asks for: the pinned model recorded beside the Fingerprint.
        "attempt_id": attempt_id,
        "fingerprint": _fingerprint(base_sha),
        "base_revision": base_sha,
        "pinned_model": pin.model,
        "effort": EFFORT,
        "provider_kwargs": {k: v for k, v in pin.kwargs.items() if k != "max_tokens"},
        "usage_flushes": [],   # one per model response, as §5 requires
        "models_seen": [],     # every model that answered — the no-swap assertion
    }

    llm = pin.build().bind_tools(TOOLSET)
    history: list[Any] = [
        SystemMessage(
            "You are implementing a change in a repository. Use the tools; the "
            "repository is the only thing you can see."
        ),
        HumanMessage(TASK),
    ]

    started = time.monotonic()
    for iteration in range(1, MAX_ITERATIONS + 1):
        message: AIMessage = llm.invoke(history)
        # §5: usage is flushed after every model response, not at Gates.
        ledger["usage_flushes"].append({"iteration": iteration, "usage": message.usage_metadata})
        ledger["models_seen"].append(
            message.response_metadata.get("model_name") or message.response_metadata.get("model")
        )
        # The whole message object goes back, never a reconstruction of it:
        # the OpenAI pin's assistant turn carries a `reasoning` block with
        # `encrypted_content` that the provider expects to see again.
        history.append(message)
        if not message.tool_calls:
            ledger["final_message"] = (
                message.text if isinstance(message.text, str) else message.text()
            )[:1500]
            ledger["stopped_because"] = "no tool call"
            break
        for call in message.tool_calls:
            requested = BY_NAME.get(call["name"])
            result = (
                requested.invoke(call["args"])
                if requested is not None
                else f"no such tool: {call['name']}"
            )
            history.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    else:
        ledger["stopped_because"] = f"iteration budget of {MAX_ITERATIONS} exhausted"

    ledger["seconds"] = round(time.monotonic() - started, 1)
    ledger["iterations"] = len(ledger["usage_flushes"])
    ledger["test_runs"] = list(TEST_RUNS)

    # The verdict is pytest's exit code on a fresh run, never the model's account.
    verdict = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=WORKSPACE, capture_output=True, text=True
    )
    ledger["independent_verdict"] = {
        "exit_code": verdict.returncode,
        "passed": verdict.returncode == 0,
        "tail": (verdict.stdout or verdict.stderr)[-500:],
    }
    ledger["diff"] = subprocess.run(
        ["git", "-C", str(WORKSPACE), "diff", "--stat", "HEAD", "--", "."],
        capture_output=True, text=True,
    ).stdout
    ledger["untracked"] = subprocess.run(
        ["git", "-C", str(WORKSPACE), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    ledger["model_swapped"] = len(set(ledger["models_seen"])) > 1
    return ledger


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", nargs="*", choices=[p.name for p in PINS],
                        default=[p.name for p in PINS])
    parser.add_argument("--scratch", default=str(HERE / ".scratch"))
    args = parser.parse_args()

    out: dict[str, Any] = {}
    for pin in [p for p in PINS if p.name in args.provider]:
        print(f"\n=== {pin.name}: {pin.model} (effort={EFFORT}) ===", flush=True)
        out[pin.name] = run(pin, Path(args.scratch))
        print(json.dumps(
            {k: v for k, v in out[pin.name].items() if k not in ("usage_flushes", "diff")},
            indent=2, default=str,
        ))

    TRANSCRIPTS.mkdir(exist_ok=True)
    path = TRANSCRIPTS / "implement.json"
    path.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
