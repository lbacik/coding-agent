from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from coding_agent.implement.prompt_cache import apply_cache_breakpoints


def _dict_block(message: BaseMessage, index: int) -> dict[str, object]:
    content = message.content
    assert isinstance(content, list)
    block = content[index]
    assert isinstance(block, dict)
    return block


def test_apply_cache_breakpoints_tags_the_end_of_the_prefix_and_the_end_of_the_messages() -> None:
    messages = [
        SystemMessage(content="attempt header"),
        SystemMessage(content="skill file"),
        HumanMessage(content="target issue"),
        AIMessage(content="thinking", tool_calls=[]),
        ToolMessage(content="tool result", tool_call_id="call-1"),
    ]

    tagged = apply_cache_breakpoints(messages, prefix_length=3)

    # The last Pinned Prefix message (index 2) is tagged...
    assert _dict_block(tagged[2], -1)["cache_control"] == {"type": "ephemeral"}
    # ...and so is the last message overall (the growing tail's end).
    assert _dict_block(tagged[4], -1)["cache_control"] == {"type": "ephemeral"}
    # Nothing else picks up a breakpoint -- untouched messages keep their
    # original plain-string content, not even converted to block shape.
    assert tagged[0].content == "attempt header"
    assert tagged[1].content == "skill file"
    assert tagged[3].content == "thinking"


def test_apply_cache_breakpoints_tags_the_text_content_but_keeps_it_readable() -> None:
    messages = [SystemMessage(content="hello")]

    tagged = apply_cache_breakpoints(messages, prefix_length=1)

    assert tagged[0].content == [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]


def test_apply_cache_breakpoints_never_mutates_the_input_messages() -> None:
    original = SystemMessage(content="hello")
    messages = [original]

    apply_cache_breakpoints(messages, prefix_length=1)

    assert original.content == "hello"
    assert messages[0] is original


def test_apply_cache_breakpoints_tags_only_once_when_the_prefix_is_the_whole_request() -> None:
    """`prefix_length == len(messages)`: the prefix breakpoint and the
    tail breakpoint land on the same message -- it must still come back
    with exactly one `cache_control` block, not a doubled-up one."""
    messages = [SystemMessage(content="hello")]

    tagged = apply_cache_breakpoints(messages, prefix_length=1)

    assert len(tagged) == 1
    assert tagged[0].content == [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]


def test_apply_cache_breakpoints_leaves_an_already_block_shaped_message_content_list_intact() -> None:
    messages = [
        SystemMessage(content="prefix"),
        AIMessage(
            content=[{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
            tool_calls=[],
        ),
    ]

    tagged = apply_cache_breakpoints(messages, prefix_length=1)

    assert _dict_block(tagged[1], 0) == {"type": "text", "text": "part one"}
    assert _dict_block(tagged[1], 1)["cache_control"] == {"type": "ephemeral"}


def test_apply_cache_breakpoints_on_an_empty_message_list_returns_empty() -> None:
    assert apply_cache_breakpoints([], prefix_length=0) == []
