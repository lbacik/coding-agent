"""A conversation-routing scripted model for Sub-agent tests.

Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (one Sub-agent per
``spawn_subagent`` call; the fixed toolset) and
`#208 <https://github.com/lbacik/coding-agent/issues/208>`_'s Testing
Decisions (one seam: whole Attempts through ``run_attempt``; the only new
test support is a conversation-routing scripted model).

Each conversation answers from its own response queue. The queue is
selected by a ``[route:<key>]`` marker the test places in the Sub-agent's
``instructions``: the Sub-agent's opening carries ``instructions`` verbatim,
so the marker reaches the first message of that conversation. A
conversation with no marker is the caller's. The model records, per
conversation, the tool names bound for it. A response can be held on a
latch until another conversation has started, so overlap at limit ``2``
and non-overlap at limit ``1`` are deterministic. Everything is guarded
by one lock, so concurrent group runners share it safely.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

ROUTE_MARKER_RE = re.compile(r"\[route:([A-Za-z0-9_-]+)\]")
"""The marker selecting a conversation's response queue (test-only syntax)."""

CALLER_ROUTE = "caller"
"""The routing key of the caller's conversation: no marker in its opening."""


def route_of(messages: Sequence[BaseMessage]) -> str:
    """The routing key of one conversation: the marker in its opening message.

    The opening is the first message of the invoked list: a Sub-agent's
    opening carries its ``instructions`` verbatim, so a test-placed
    ``[route:<key>]`` marker selects that conversation's queue. No marker
    means the caller's conversation.
    """
    if not messages:
        return CALLER_ROUTE
    first = messages[0]
    if isinstance(first, HumanMessage) and isinstance(first.content, str):
        match = ROUTE_MARKER_RE.search(first.content)
        if match is not None:
            return match.group(1)
    return CALLER_ROUTE


class RoutedScriptedModel:
    """One scripted model answering every conversation from its own queue.

    ``caller`` is the caller's queue; ``routes`` maps each ``[route:<key>]``
    marker to that Sub-agent conversation's queue. Every ``invoke`` pops
    the head of the routed queue. ``latches`` optionally holds a response:
    ``latches[(route, index)]`` is a :class:`threading.Event` waited on
    before the ``index``-th response (zero-based, counting only responses
    popped for ``route``) of that conversation is returned. ``started``,
    in first-invoke order, and ``started_events`` let a test wait until
    another conversation has started. ``bound_tool_names`` records, per
    conversation, the tool names of the ``bind_tools`` handle that
    conversation invoked through — so tests assert structurally, on the
    bytes actually handed to the model, that a Sub-agent bound exactly
    the fixed set.
    """

    def __init__(
        self,
        caller: Sequence[AIMessage],
        routes: Mapping[str, Sequence[AIMessage]] | None = None,
        *,
        latches: Mapping[tuple[str, int], threading.Event] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, list[AIMessage]] = {
            CALLER_ROUTE: list(caller),
        }
        for key, outcomes in (routes or {}).items():
            self._queues[key] = list(outcomes)
        self._latches: dict[tuple[str, int], threading.Event] = dict(latches or {})
        self._popped: dict[str, int] = {key: 0 for key in self._queues}
        self._started: list[str] = []
        self.started_events: dict[str, threading.Event] = {}
        self.bound_tool_names: dict[str, list[str]] = {}
        self.invocations: list[tuple[str, tuple[BaseMessage, ...]]] = []
        self.bind_history: list[tuple[str | None, list[str]]] = []

    def wait_started(self, route: str, timeout: float = 10.0) -> bool:
        """Wait until ``route`` has started (its first ``invoke``)."""
        with self._lock:
            event = self.started_events.get(route)
            if event is None:
                event = threading.Event()
                self.started_events[route] = event
        return event.wait(timeout)

    @property
    def started(self) -> list[str]:
        """Routing keys in first-invoke order, oldest first."""
        with self._lock:
            return list(self._started)

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> _BoundRoutedModel:
        """Bind ``tools`` and return the handle that conversation invokes."""
        names = [tool.name for tool in tools]
        with self._lock:
            self.bind_history.append((tool_choice, list(names)))
        return _BoundRoutedModel(self, tuple(names))

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        """Direct invocation without a bound toolset (records no tool names)."""
        return self._pop((), input, record_names=False)

    def _pop(
        self,
        bound_names: Sequence[str],
        input: LanguageModelInput,
        *,
        record_names: bool,
    ) -> BaseMessage:
        """Pop the routed queue's head, honouring that response's latch."""
        assert isinstance(input, Sequence)
        messages = [m for m in input if isinstance(m, BaseMessage)]
        route = route_of(messages)
        with self._lock:
            if route not in self._started:
                self._started.append(route)
                event = self.started_events.get(route)
                if event is None:
                    event = threading.Event()
                    self.started_events[route] = event
                event.set()
            if route not in self._queues:
                raise AssertionError(f"no scripted queue for route {route!r}")
            if record_names and route not in self.bound_tool_names:
                self.bound_tool_names[route] = list(bound_names)
            index = self._popped.get(route, 0)
            queue = self._queues[route]
            if not queue:
                raise AssertionError(f"scripted queue for route {route!r} is empty")
            latch = self._latches.get((route, index))
            response = queue.pop(0)
            self._popped[route] = index + 1
            self.invocations.append((route, tuple(messages)))
        if latch is not None:
            latch.wait()
        return response


class _BoundRoutedModel:
    """One ``bind_tools`` handle: shares the queues, carries its tool names.

    ``bind_tools`` on the shared model returns a new handle per call, so the
    conversation that invokes through it is attributed exactly the tool
    names bound for it — even where binds interleave. Handles share the
    queues, latches, started records and invocation log through the shared
    model's one lock; only the bound names differ per handle. Never
    constructed directly: use :meth:`RoutedScriptedModel.bind_tools`.
    """

    def __init__(self, shared: RoutedScriptedModel, names: tuple[str, ...]) -> None:
        self._shared = shared
        self._names = names

    def bind_tools(
        self, tools: Sequence[BaseTool], tool_choice: str | None = None
    ) -> _BoundRoutedModel:
        """Rebind through the shared model: a fresh handle with fresh names."""
        return self._shared.bind_tools(tools, tool_choice)

    def invoke(self, input: LanguageModelInput) -> BaseMessage:
        """Pop the routed queue's head, recording this handle's tool names."""
        return self._shared._pop(self._names, input, record_names=True)
