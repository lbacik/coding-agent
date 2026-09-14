from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from coding_agent.github.issues import TargetIssue
from coding_agent.implement.loop import build_opening_messages, run_tool_loop
from coding_agent.implement.pinned_prefix import InjectedFile, PinnedPrefix
from conftest import FakeChatModel


@tool
def fake_write_file(path: str, content: str) -> str:
    """Records a write for the test to inspect."""
    return f"wrote {path!r}"


def _ai_message(*, tool_calls: list[dict[str, object]] | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=tool_calls or [],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


# --- well-formed tool call round trip ---------------------------------------


def test_run_tool_loop_executes_a_tool_call_then_stops() -> None:
    call_response = _ai_message(
        tool_calls=[
            {
                "name": "fake_write_file",
                "args": {"path": "a.txt", "content": "hi"},
                "id": "call-1",
                "type": "tool_call",
            }
        ]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    result = run_tool_loop(model, [fake_write_file], [SystemMessage(content="hello")])

    assert result.tool_call_count == 1
    assert result.conversation[0] == SystemMessage(content="hello")
    assert result.conversation[1] is call_response
    tool_message = result.conversation[2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.content == "wrote 'a.txt'"
    assert result.conversation[3] is final_response

    # The second invoke saw the tool result appended to the conversation.
    assert model.invocations[1][-1] is tool_message


def test_run_tool_loop_stops_immediately_when_no_tool_is_called() -> None:
    final_response = AIMessage(content="nothing to do", tool_calls=[])
    model = FakeChatModel([final_response])

    result = run_tool_loop(model, [fake_write_file], [SystemMessage(content="hello")])

    assert result.tool_call_count == 0
    assert result.conversation == (SystemMessage(content="hello"), final_response)


def test_run_tool_loop_reports_an_unknown_tool_without_raising() -> None:
    call_response = _ai_message(
        tool_calls=[{"name": "no_such_tool", "args": {}, "id": "call-1", "type": "tool_call"}]
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([call_response, final_response])

    result = run_tool_loop(model, [fake_write_file], [SystemMessage(content="hello")])

    tool_message = result.conversation[2]
    assert isinstance(tool_message, ToolMessage)
    assert "no such tool" in str(tool_message.content)


# --- ADR 0010: the assistant turn survives verbatim, opaque blocks included --


def test_run_tool_loop_appends_an_opaque_block_assistant_turn_verbatim() -> None:
    """A response carrying a provider-specific block (e.g. encrypted
    `reasoning` content on the Responses API) must come back exactly as
    received — this loop never rebuilds it from `.content`/`.tool_calls`."""
    opaque_response = AIMessage(
        content=[
            {"type": "reasoning", "encrypted_content": "opaque-blob-xyz"},
            {"type": "text", "text": "thinking..."},
        ],
        tool_calls=[
            {
                "name": "fake_write_file",
                "args": {"path": "a.txt", "content": "hi"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
        additional_kwargs={"some_provider_field": "must-not-be-dropped"},
    )
    final_response = AIMessage(content="done", tool_calls=[])
    model = FakeChatModel([opaque_response, final_response])

    result = run_tool_loop(model, [fake_write_file], [SystemMessage(content="hello")])

    assert result.conversation[1] is opaque_response
    # The same opaque object, unmodified, is what the second call actually sent back.
    assert model.invocations[1][1] is opaque_response
    stored = result.conversation[1]
    assert isinstance(stored, AIMessage)
    assert stored.additional_kwargs == {"some_provider_field": "must-not-be-dropped"}
    assert stored.content == opaque_response.content


# --- build_opening_messages ---------------------------------------------------


def test_build_opening_messages_lays_out_header_files_and_issue_as_separate_elements() -> None:
    prefix = PinnedPrefix(
        attempt_header="# Attempt Header\n...",
        injected_files=(
            InjectedFile(label="implement/SKILL.md", text="implement skill text"),
            InjectedFile(label="tdd/SKILL.md", text="tdd skill text"),
        ),
    )
    issue = TargetIssue(number=34, title="Do the thing", body="Body text")

    messages = build_opening_messages(prefix, issue)

    assert len(messages) == 4
    assert messages[0] == SystemMessage(content=prefix.attempt_header)
    assert isinstance(messages[1], SystemMessage)
    assert "implement/SKILL.md" in str(messages[1].content)
    assert "implement skill text" in str(messages[1].content)
    assert isinstance(messages[2], SystemMessage)
    assert "tdd/SKILL.md" in str(messages[2].content)
    last = messages[3]
    assert "#34" in str(last.content)
    assert "Do the thing" in str(last.content)
    assert "Body text" in str(last.content)
