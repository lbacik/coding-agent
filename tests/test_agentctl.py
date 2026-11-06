"""``agentctl`` grammar, envelope and exit codes (issue #247).

Contract: agent-operator-contract major 1 at ``06ee46c``, §4, §4.1, §5, §6.
Reference shapes: ``examples/agentctl/`` fixtures at that revision.

These tests call the entry point in-process with argv and assert only
stdout and the exit code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coding_agent import agentctl


def run_call(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = agentctl.main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, captured.out


def parse_envelope(stdout: str) -> dict[str, Any]:
    assert stdout.endswith("\n")
    assert stdout.count("\n") == 1
    envelope: dict[str, Any] = json.loads(stdout)
    return envelope


def test_without_contract_answers_usage(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in ([], ["status"], ["--contract"]):
        code, out = run_call(list(argv), capsys)
        assert code == 2
        envelope = parse_envelope(out)
        assert envelope["contract"] == "1.0"
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "usage"


def test_contract_mismatch_executes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_call(["--contract", "2", "status"], capsys)
    assert code == 4
    assert out == (
        '{"contract":"1.0","error":{"code":"contract_mismatch",'
        '"details":{"supported":[1]},'
        '"message":"this instance speaks contract major 1",'
        '"retryable":false},"ok":false}\n'
    )
    # Contract is checked first: even a malformed operation still mismatches.
    code2, out2 = run_call(["--contract", "2", "bogus-op", "--bad"], capsys)
    assert code2 == 4
    assert json.loads(out2)["error"]["code"] == "contract_mismatch"


def test_usage_fixture_shape(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_call(["--contract", "1", "stop", "--after"], capsys)
    assert code == 2
    assert out == (
        '{"contract":"1.0","error":{"code":"usage",'
        '"message":"argument --after: expected one argument",'
        '"retryable":false},"ok":false}\n'
    )


def test_unsupported_fixture_shape(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--contract",
        "1",
        "handoff",
        "now",
        "--request-id",
        "req-5e6f7a8b9c0d",
        "--expect-repo",
        "example-owner/example-repo",
    ]
    code, out = run_call(argv, capsys)
    assert code == 3
    assert out == (
        '{"contract":"1.0","error":{"code":"unsupported",'
        '"details":{"capability":"operator-handoff"},'
        '"message":"this instance does not advertise operator-handoff",'
        '"request_id":"req-5e6f7a8b9c0d","retryable":false},"ok":false}\n'
    )


def test_agent_unreachable_fixture_shape(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--contract",
        "1",
        "stop",
        "--request-id",
        "req-1a2b3c4d5e6f",
        "--expect-repo",
        "example-owner/example-repo",
    ]
    code, out = run_call(argv, capsys)
    assert code == 6
    assert out == (
        '{"contract":"1.0","error":{"code":"agent_unreachable",'
        '"message":"cannot reach the agent process; the outcome is unknown, '
        'retry with the same request ID",'
        '"request_id":"req-1a2b3c4d5e6f","retryable":true},"ok":false}\n'
    )


def test_exit_codes_are_distinct_per_outcome() -> None:
    assert agentctl.EXIT_FOR_CODE["rejected"] == 1
    assert agentctl.EXIT_FOR_CODE["repository_mismatch"] == 1
    assert agentctl.EXIT_FOR_CODE["payload_mismatch"] == 1
    assert agentctl.EXIT_FOR_CODE["invalid_argument"] == 1
    assert agentctl.EXIT_FOR_CODE["usage"] == 2
    assert agentctl.EXIT_FOR_CODE["unsupported"] == 3
    assert agentctl.EXIT_FOR_CODE["contract_mismatch"] == 4
    assert agentctl.EXIT_FOR_CODE["not_found"] == 5
    assert agentctl.EXIT_FOR_CODE["agent_unreachable"] == 6
    assert agentctl.EXIT_FOR_CODE["internal"] == 7
    codes = set(agentctl.EXIT_FOR_CODE.values())
    assert codes == {1, 2, 3, 4, 5, 6, 7}


@pytest.mark.parametrize(
    "argv",
    [
        ["--contract", "1", "handoff", "now", "--request-id", "req-a1", "--expect-repo", "o/r"],
        [
            "--contract",
            "1",
            "recovery",
            "retry",
            "#1/1",
            "--request-id",
            "req-a2",
            "--expect-repo",
            "o/r",
        ],
        [
            "--contract",
            "1",
            "recovery",
            "release",
            "#1/1",
            "--saved-at",
            "/tmp/x",
            "--request-id",
            "req-a3",
            "--expect-repo",
            "o/r",
        ],
        ["--contract", "1", "errors", "reset", "--request-id", "req-a4", "--expect-repo", "o/r"],
    ],
)
def test_unsupported_operations_store_nothing(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run_call(list(argv), capsys)
    assert code == 3
    envelope = parse_envelope(out)
    assert envelope["error"]["code"] == "unsupported"
    assert "capability" in envelope["error"]["details"]


def test_capability_gating_comes_before_argument_validation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Invalid request-id and repo values still answer unsupported for
    # unadvertised capabilities: capability precedes values, repository
    # and idempotency checks.
    argv = [
        "--contract",
        "1",
        "handoff",
        "now",
        "--request-id",
        "bad id!",
        "--expect-repo",
        "not-a-repo",
    ]
    code, out = run_call(argv, capsys)
    assert code == 3
    assert parse_envelope(out)["error"]["code"] == "unsupported"

    # ``stop --after 0`` is unsupported, not invalid_argument.
    argv2 = [
        "--contract",
        "1",
        "stop",
        "--after",
        "0",
        "--request-id",
        "req-b1",
        "--expect-repo",
        "o/r",
    ]
    code2, out2 = run_call(argv2, capsys)
    assert code2 == 3
    assert parse_envelope(out2)["error"]["details"] == {"capability": "stop-after"}


def test_stop_after_and_next_issue_unsupported_pool_never_advertised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "pool" not in agentctl.ADVERTISED_CAPABILITIES
    assert "stop-after" not in agentctl.ADVERTISED_CAPABILITIES
    assert "next-issue" in agentctl.ADVERTISED_CAPABILITIES

    code, out = run_call(
        [
            "--contract",
            "1",
            "stop",
            "--after",
            "3",
            "--request-id",
            "req-c1",
            "--expect-repo",
            "o/r",
        ],
        capsys,
    )
    assert code == 3
    assert parse_envelope(out)["error"]["details"] == {"capability": "stop-after"}

    # ``next issue`` is advertised: without an engine it answers
    # ``agent_unreachable`` (exit 6), not ``unsupported``.
    code2, out2 = run_call(
        [
            "--contract",
            "1",
            "next",
            "issue",
            "43",
            "--request-id",
            "req-c2",
            "--expect-repo",
            "o/r",
        ],
        capsys,
    )
    assert code2 == 6
    assert parse_envelope(out2)["error"]["code"] == "agent_unreachable"

    # ``pool`` is declaration-only: there is no pool operation.
    code3, out3 = run_call(["--contract", "1", "pool"], capsys)
    assert code3 == 2
    assert parse_envelope(out3)["error"]["code"] == "usage"


def test_same_bad_call_always_fails_same_check(capsys: pytest.CaptureFixture[str]) -> None:
    # Grammar (usage) beats capability: missing ``now`` is usage, not unsupported.
    code, out = run_call(["--contract", "1", "handoff"], capsys)
    assert code == 2
    assert parse_envelope(out)["error"]["code"] == "usage"

    # Values (invalid_argument) beat the engine: a bad request-id never
    # reaches agent_unreachable.
    code2, out2 = run_call(
        ["--contract", "1", "stop", "--request-id", "bad!", "--expect-repo", "o/r"],
        capsys,
    )
    assert code2 == 1
    assert parse_envelope(out2)["error"]["code"] == "invalid_argument"

    code3, out3 = run_call(
        ["--contract", "1", "stop", "--request-id", "req-d1", "--expect-repo", "no-slash"],
        capsys,
    )
    assert code3 == 1
    assert parse_envelope(out3)["error"]["code"] == "invalid_argument"

    # Determinism: repeat the same bad calls.
    for _ in range(2):
        c, o = run_call(["--contract", "1", "handoff"], capsys)
        assert (c, o) == (code, out)


def test_core_operations_without_engine_answer_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (
        ["--contract", "1", "identity"],
        ["--contract", "1", "status"],
        ["--contract", "1", "command", "req-000000000000"],
        ["--contract", "1", "attempts", "list"],
        ["--contract", "1", "attempts", "show", "#41/1"],
        ["--contract", "1", "resume", "--request-id", "req-e1", "--expect-repo", "o/r"],
    ):
        code, out = run_call(list(argv), capsys)
        assert code == 6
        envelope = parse_envelope(out)
        assert envelope["error"]["code"] == "agent_unreachable"
        assert envelope["error"]["retryable"] is True


def test_attempts_list_limit_values(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_call(["--contract", "1", "attempts", "list", "--limit", "0"], capsys)
    assert code == 1
    assert parse_envelope(out)["error"]["code"] == "invalid_argument"

    code2, out2 = run_call(
        ["--contract", "1", "attempts", "list", "--limit", "101"], capsys
    )
    assert code2 == 1
    assert parse_envelope(out2)["error"]["code"] == "invalid_argument"

    code3, out3 = run_call(
        ["--contract", "1", "attempts", "list", "--limit", "20"], capsys
    )
    assert code3 == 6


def test_image_carries_no_discovery_label() -> None:
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
        encoding="utf-8"
    )
    for line in dockerfile.splitlines():
        assert not line.strip().startswith("LABEL")
    assert "io.github.lbacik.agent-operator" not in dockerfile
    assert "io.github.lbacik.simple-coding-agent.role" not in dockerfile
