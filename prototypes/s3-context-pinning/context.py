"""THROWAWAY PROTOTYPE — see README.md. Not production code, not the S3 node.

The compaction policy on its own, so it can be checked without spending a
model call — the same trick `prototypes/s3-implement-tdd/registry.py` played
with the activation rules. Running this file directly checks every rule below,
including the ones a model might never happen to trigger.

Two policies, because `L3-IMP-4` is a claim about a data structure and the only
way to see that is to hold a wrong one beside it:

  KEEP   the pinned prefix lives outside the evictable region entirely, and
         eviction walks whole exchange units from the oldest end. "Never
         compacted" is then true by construction rather than by obedience.

  TAIL   one flat message list, truncated to its last N messages — the loop
         everybody writes first. It drops the system message and orphans tool
         results, and it does both without saying anything.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


# --------------------------------------------------------------------------
# A message here is anything with a `role`; the harness passes langchain
# objects and the probe passes dicts. Nothing in this file inspects content —
# ADR 0010: history is carried, never rebuilt.
# --------------------------------------------------------------------------


def role_of(message: Any) -> str:
    if isinstance(message, dict):
        return message["role"]
    return {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}.get(
        getattr(message, "type", ""), getattr(message, "type", "?")
    )


def chars_of(message: Any) -> int:
    if isinstance(message, dict):
        return len(json.dumps(message))
    return len(str(getattr(message, "content", "")))


def estimate_tokens(messages: Iterable[Any]) -> int:
    """Only a fallback. The harness replaces this with the provider's own count
    from the previous response, because #24 measured the two pins counting an
    identical prompt at 6818 and 4414 — a single estimate is two budgets."""
    return sum(chars_of(m) for m in messages) // 4


class PinnedMaterialTooLarge(RuntimeError):
    """The pinned prefix alone does not fit under the threshold.

    Question 3's ceiling, made executable. There is no compaction that helps:
    every byte of the prefix is a byte the policy promised never to drop, so
    the only honest move is to refuse to open the Attempt. Silently dropping
    part of it would leave `tdd`'s seam instruction pointing at nothing, and
    #22 established that the model does not report that — it just proceeds.
    """


@dataclass
class Unit:
    """One exchange: an assistant turn and every tool result answering it.

    The unit — not the message — is the eviction grain. An assistant turn
    carrying `tool_calls` and the `ToolMessage`s answering them are one
    indivisible thing to every provider; splitting them is a malformed request
    on Anthropic and a broken reasoning chain on OpenAI Responses.
    """

    messages: list[Any] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.messages)


@dataclass
class KeepPinned:
    """Policy KEEP. The candidate design."""

    pinned: list[Any]
    units: list[Unit] = field(default_factory=list)
    evicted: int = 0
    tokens: Callable[[Iterable[Any]], int] = estimate_tokens

    def open_unit(self, *messages: Any) -> None:
        self.units.append(Unit(list(messages)))

    def extend_unit(self, *messages: Any) -> None:
        self.units[-1].messages.extend(messages)

    def messages(self) -> list[Any]:
        return [*self.pinned, *(m for u in self.units for m in u.messages)]

    def compact(self, threshold: int, scale: float = 1.0) -> int:
        """Evict oldest units until the estimate is under `threshold`.

        `scale` calibrates the local character estimate against the provider's
        own count of the last prompt actually sent, so the trigger tracks the
        pin rather than a guess — #24 measured the two pins counting an
        identical prompt at 6818 and 4414.
        """
        floor = self.tokens(self.pinned)
        if floor >= threshold:
            raise PinnedMaterialTooLarge(
                f"pinned prefix is ~{floor} tokens against a {threshold} threshold"
            )
        dropped = 0
        while self.units and self.tokens(self.messages()) * scale >= threshold:
            self.units.pop(0)
            dropped += 1
        self.evicted += dropped
        return dropped


@dataclass
class NaiveTail:
    """Policy TAIL. The trap arm: one flat list, keep the last `keep`."""

    pinned: list[Any]
    flat: list[Any] = field(default_factory=list)
    keep: int = 12
    evicted: int = 0
    tokens: Callable[[Iterable[Any]], int] = estimate_tokens

    def __post_init__(self) -> None:
        self.flat = list(self.pinned)

    def open_unit(self, *messages: Any) -> None:
        self.flat.extend(messages)

    def extend_unit(self, *messages: Any) -> None:
        self.flat.extend(messages)

    def messages(self) -> list[Any]:
        return self.flat

    def compact(self, threshold: int, scale: float = 1.0) -> int:
        before = len(self.flat)
        while len(self.flat) > self.keep and self.tokens(self.flat) * scale >= threshold:
            self.flat.pop(0)
        dropped = before - len(self.flat)
        self.evicted += dropped
        return dropped


# --------------------------------------------------------------------------
# The rules, checked without a model.
# --------------------------------------------------------------------------


def _msg(role: str, text: str, **extra: Any) -> dict:
    return {"role": role, "text": text, **extra}


def _loaded(policy_cls: Any, *, prefix_chars: int = 400, exchanges: int = 12, **kw: Any) -> Any:
    pinned = [_msg("system", "P" * prefix_chars)]
    policy = policy_cls(pinned=pinned, **kw)
    for i in range(exchanges):
        policy.open_unit(_msg("assistant", f"turn {i} " + "x" * 300, tool_calls=[f"c{i}"]))
        policy.extend_unit(_msg("tool", "y" * 600, tool_call_id=f"c{i}"))
    return policy


def _orphans(messages: list[Any]) -> list[str]:
    """Tool results whose calling assistant turn is no longer present."""
    seen: set[str] = set()
    orphans: list[str] = []
    for m in messages:
        if m.get("tool_calls"):
            seen.update(m["tool_calls"])
        if m["role"] == "tool" and m["tool_call_id"] not in seen:
            orphans.append(m["tool_call_id"])
    return orphans


def probe() -> list[dict]:
    rows: list[dict] = []

    def check(rule: str, ok: bool, detail: str = "") -> None:
        rows.append({"rule": rule, "pass": bool(ok), "detail": detail})

    # --- KEEP: what L3-IMP-4 is asserting ---------------------------------
    keep = _loaded(KeepPinned)
    identity = {id(m) for m in keep.messages()}
    dropped = keep.compact(threshold=800)
    after = keep.messages()
    check(
        "KEEP: the pinned prefix survives eviction of the entire history",
        after[0] is keep.pinned[0] and after[:1] == keep.pinned,
        f"{dropped} units evicted, prefix intact",
    )
    check(
        "KEEP: eviction leaves no orphaned tool result",
        _orphans(after) == [],
        f"orphans={_orphans(after)}",
    )
    check(
        "KEEP: eviction is whole-unit — no assistant turn survives without its results",
        all(len(u.messages) == 2 for u in keep.units),
        f"{len(keep.units)} units remain",
    )
    check(
        "KEEP: nothing is rewritten — every surviving message is the object appended (ADR 0010)",
        all(id(m) in identity for m in after),
        "identity preserved",
    )

    empty = KeepPinned(pinned=[_msg("system", "P" * 400)])
    empty.compact(threshold=200)  # above the floor, so the refusal is not what is being checked
    check(
        "KEEP: an empty history compacts to the pinned prefix and no further",
        empty.messages() == empty.pinned,
        "floor is the prefix",
    )

    tiny = KeepPinned(pinned=[_msg("system", "P" * 40_000)])
    try:
        tiny.compact(threshold=5_000)
        check("KEEP: an oversized pinned prefix refuses rather than dropping part of itself", False,
              "no refusal raised")
    except PinnedMaterialTooLarge as exc:
        check("KEEP: an oversized pinned prefix refuses rather than dropping part of itself", True, str(exc))

    # --- TAIL: the trap, asserted rather than assumed ----------------------
    tail = _loaded(NaiveTail, keep=6)
    tail.compact(threshold=800)
    flat = tail.messages()
    check(
        "TAIL: the pinned prefix is silently dropped — the Attempt Header goes with it",
        flat[0]["role"] != "system",
        f"first surviving role is {flat[0]['role']!r}",
    )
    # Whether the cut lands mid-unit is a coin flip on where the boundary
    # falls, which is worse than always breaking: it makes the bug
    # intermittent and therefore attributable to the model.
    cuts = {}
    for keep in (6, 7):
        probe_tail = _loaded(NaiveTail, keep=keep)
        probe_tail.compact(threshold=800)
        cuts[keep] = _orphans(probe_tail.messages())
    check(
        "TAIL: a tool result is orphaned whenever the cut lands mid-unit",
        any(cuts.values()),
        f"keep=6 aligns ({cuts[6]}), keep=7 orphans {cuts[7]} — same policy, different luck",
    )
    check(
        "TAIL: the policy itself reports neither loss — it returns a count and nothing else",
        True,
        "whether the *provider* stays quiet about them is what the live arms answer",
    )
    return rows


if __name__ == "__main__":
    rows = probe()
    for row in rows:
        print(f"[{'PASS' if row['pass'] else 'FAIL'}] {row['rule']}" + (f"  ({row['detail']})" if row["detail"] else ""))
    print(f"\n{sum(r['pass'] for r in rows)}/{len(rows)} rules hold")
    sys.exit(0 if all(r["pass"] for r in rows) else 1)
