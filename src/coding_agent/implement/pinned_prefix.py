from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from langchain_core.tools import BaseTool

from coding_agent.provider.pinned_model import PinnedModel
from coding_agent.skillbundle.verify import BUNDLE_SPEC

#: S3.4 (issue #33): the implementer's injected set, in the order they are
#: laid down in the Pinned Prefix -- `implement` itself, `tdd` nested under
#: it, `codebase-design` nested under `tdd`, then `tdd`'s own two Companion
#: Files. `code-review` is a Skill Bundle member but never part of an
#: implementer's Pinned Prefix (ADR 0012). The companion relative paths are
#: read from `BUNDLE_SPEC` rather than repeated here, so there is one
#: manifest for "which companions exist" across both verification and
#: composition.
_INJECTED_FILE_MANIFEST: tuple[tuple[str, str], ...] = (
    ("implement", "SKILL.md"),
    ("tdd", "SKILL.md"),
    ("codebase-design", "SKILL.md"),
    *(("tdd", relative) for relative in BUNDLE_SPEC.companion_files["tdd"]),
)


@dataclass(frozen=True)
class AttemptFacts:
    """The Attempt's own facts the Attempt Header carries (CONTEXT.md's
    Attempt Header entry): what identifies and pins this Attempt, the Seam
    Set `confirm_seam` already confirmed before this conversation opened
    (ADR 0008), and the Target Project's language, which decides whether
    `tdd`'s injected companion idioms actually match this Attempt."""

    issue_number: int
    issue_title: str
    fingerprint: str
    base_revision: str
    seam_set: tuple[str, ...]
    target_language: str


@dataclass(frozen=True)
class InjectedFile:
    """One whole, verbatim file laid into the Pinned Prefix. `label` is
    "<skill>/<relative path>", e.g. "tdd/mocking.md" -- never the file's
    content, so a test asserting "exactly once" can key off it without
    parsing prose."""

    label: str
    text: str


@dataclass(frozen=True)
class PinnedPrefix:
    """The Attempt Header plus the injected skill files (issue #33's slice
    of the Pinned Prefix; the Target Issue itself is a separate element of
    that region, composed where the conversation is opened). `injected_files`
    is exactly `_INJECTED_FILE_MANIFEST`, in order -- three `SKILL.md` files
    and `tdd`'s two Companion Files, each present once, byte-for-byte as
    installed (ADR 0007), and never `codebase-design`'s own companions
    (ADR 0012)."""

    attempt_header: str
    injected_files: tuple[InjectedFile, ...]

    @property
    def rendered(self) -> str:
        """The whole prefix as one string, in the order it is laid into the
        conversation. Only ever used for a human to inspect by eye or for
        `estimated_tokens` below -- the loop that actually opens the
        conversation (a later ticket) sends the header and each file as
        their own pinned elements, not this concatenation."""
        parts = [self.attempt_header]
        parts.extend(f"----- {f.label} -----\n{f.text}" for f in self.injected_files)
        return "\n\n".join(parts)

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.rendered)


def compose_attempt_header(facts: AttemptFacts) -> str:
    """The framing the Worker itself authors (ADR 0007) -- never an edit to
    any injected file. Carries the Seam Set (`L3-IMP-15`) and answers, in
    prose, the two places the injected text below refers to something it
    does not itself contain: `codebase-design`'s own "Going deeper"
    companions are unavailable in an Attempt (ADR 0012), and `tdd`'s
    companion examples are all TypeScript/jest while this Target Project's
    language may not be -- the principles carry across languages, the
    syntax does not."""
    seam_set_text = ", ".join(f"`{seam}`" for seam in facts.seam_set)
    language_note = (
        "this Attempt happens to match"
        if facts.target_language == "typescript"
        else f"translate the pattern, not the syntax, to {facts.target_language}"
    )
    return (
        "# Attempt Header\n\n"
        f"Target Issue: #{facts.issue_number} {facts.issue_title!r}\n"
        f"Fingerprint: {facts.fingerprint}\n"
        f"Base Revision: {facts.base_revision}\n"
        f"Target Project language: {facts.target_language}\n\n"
        "Seam Set (ADR 0008): the public boundaries your tests may be written "
        f"at, already confirmed before this conversation opened -- {seam_set_text}. "
        "Never re-derive or renegotiate it.\n\n"
        "Two things the skill files below refer to but do not themselves "
        "contain, answered here rather than edited into their text "
        "(ADR 0007):\n\n"
        "- `codebase-design`'s own \"Going deeper\" companion files "
        "(`DEEPENING.md`, `DESIGN-IT-TWICE.md`) are unavailable in this "
        "Attempt. Its `SKILL.md`, injected below, is the whole of what you "
        "have of it.\n"
        "- `tdd`'s two companion files, also injected below, illustrate "
        f"every example in TypeScript and jest. This Target Project's "
        f"language is {facts.target_language}: the principles in both files "
        f"apply regardless of language, and the syntax does not -- "
        f"{language_note}.\n\n"
        "Use `find_files` or `search_text` before broad directory walking or "
        "whole-file reads. Once a region is known, use `read_lines` with its "
        "stable one-based references.\n\n"
        "The three `SKILL.md` files and `tdd`'s two companion files follow, "
        "verbatim and in full, exactly as installed."
    )


def compose_pinned_prefix(skills_dir: Path, facts: AttemptFacts) -> PinnedPrefix:
    """Reads `_INJECTED_FILE_MANIFEST` whole and verbatim from `skills_dir`
    (the Skill Bundle's base store, e.g. `<home>/.agents/skills`) and pairs
    it with an Attempt Header the Worker authors for `facts`. Performs no
    normalisation, no partial reads, no edits -- cut-and-concatenate only
    (ADR 0007)."""
    injected_files = tuple(
        InjectedFile(
            label=f"{skill}/{relative}",
            text=(skills_dir / skill / relative).read_text(encoding="utf-8"),
        )
        for skill, relative in _INJECTED_FILE_MANIFEST
    )
    return PinnedPrefix(attempt_header=compose_attempt_header(facts), injected_files=injected_files)


def estimate_tokens(text: str) -> int:
    """A rough, provider-agnostic estimate -- roughly 4 bytes per token for
    English prose -- never the exact count a Pinned Model would report,
    which is a per-provider concern the tool loop establishes later. Good
    enough for a guard whose point is that a correct run never approaches
    it (ADR 0011)."""
    return max(1, len(text.encode("utf-8")) // 4)


#: Kept as a source-compatible alias for callers that supplied the retired
#: eviction threshold table. New Attempt code uses `ContextWindowTable`, whose
#: entries also carry the safety margin, request overhead and handoff cap.
CompactionThresholdTable = dict[str, int]

from coding_agent.implement.context_handoff import ContextWindowConfig

ContextWindowTable = dict[str, ContextWindowConfig]


class PinnedPrefixTooLarge(Exception):
    """`L3-IMP-13`: either the Pinned Prefix alone crosses the compaction
    threshold configured for its Pinned Model, or that Pinned Model has no
    configured threshold at all. Raised before any model call and before
    the Attempt opens -- never resolved by dropping part of the prefix, the
    same refusal ADR 0009 makes for a Pinned Model without a price: nothing
    is claimed yet, so nothing is stranded (ADR 0011 part 4)."""


def assert_within_compaction_threshold(
    prefix: PinnedPrefix, pin: PinnedModel, table: CompactionThresholdTable
) -> None:
    """Refuses -- raises, makes no model call -- where `pin` has no entry in
    `table`, or `prefix` alone estimates over the entry it does have.
    Checked before anything else about opening the Attempt; the actual
    model call and tool loop are a later ticket."""
    threshold = table.get(pin.key)
    if threshold is None:
        raise PinnedPrefixTooLarge(
            f"no compaction threshold configured for pinned model {pin.key!r}; "
            "refusing to open the Attempt"
        )
    estimated = prefix.estimated_tokens
    if estimated > threshold:
        raise PinnedPrefixTooLarge(
            f"the Pinned Prefix is ~{estimated} estimated tokens, over the "
            f"{threshold}-token compaction threshold configured for {pin.key!r}; "
            "refusing to open the Attempt"
        )


def context_window_for(
    pin: PinnedModel, table: ContextWindowTable | CompactionThresholdTable
) -> ContextWindowConfig | None:
    """Resolve new context policy while accepting the retired integer table."""
    value = table.get(pin.key)
    if isinstance(value, ContextWindowConfig):
        return value
    if isinstance(value, int):
        return ContextWindowConfig(threshold_tokens=value, safety_margin_tokens=0, request_overhead_tokens=0)
    return None


def assert_within_context_window(
    prefix: PinnedPrefix, pin: PinnedModel, table: ContextWindowTable | CompactionThresholdTable
) -> ContextWindowConfig:
    """Refuse an absent or non-positive per-pin window before model work."""
    if isinstance(table.get(pin.key), int):
        assert_within_compaction_threshold(
            prefix, pin, cast(CompactionThresholdTable, table)
        )  # compatibility diagnostics
    try:
        config = context_window_for(pin, table)
    except ValueError as exc:
        raise PinnedPrefixTooLarge(str(exc)) from exc
    if config is None:
        raise PinnedPrefixTooLarge(
            f"no context-window configuration for pinned model {pin.key!r}; refusing to open the Attempt"
        )
    if prefix.estimated_tokens > config.usable_threshold:
        raise PinnedPrefixTooLarge(
            f"the Pinned Prefix is ~{prefix.estimated_tokens} estimated tokens, over the "
            f"{config.usable_threshold}-token usable context window configured for {pin.key!r}; "
            "refusing to open the Attempt"
        )
    return config
class SkillPathResolverPresent(Exception):
    """`L3-IMP-14`: a tool in the bound toolset can resolve an arbitrary path
    under a skill's own directory -- the shape ADR 0012 retired
    (`read_skill_resource(skill, path)`). No tool in this codebase has this
    shape today; the assertion exists so a later addition trips here,
    structurally, rather than becoming a companion that silently applies
    outside the reviewed set."""


# Deliberately naive, the same trade-off `skillbundle.verify.SKILL_REF_RE`
# makes: a substring match on argument names, not an inspection of what a
# tool's implementation actually does. It will flag a tool merely shaped
# like a resolver and miss one whose parameters are named unrecognisably --
# the true guarantee is that this codebase's toolset is reviewed at all
# (nothing here executes a tool to find out what it touches), and this
# assertion is the structural trip-wire for the one shape ADR 0012 named by
# name, not a general-purpose static analyser.
_SKILL_NAME_MARKERS = ("skill",)
_PATH_NAME_MARKERS = ("path", "file")


def _resolves_a_skill_path(tool: BaseTool) -> bool:
    param_names = [name.lower() for name in tool.args]
    has_skill_param = any(marker in name for name in param_names for marker in _SKILL_NAME_MARKERS)
    has_path_param = any(marker in name for name in param_names for marker in _PATH_NAME_MARKERS)
    return has_skill_param and has_path_param


def assert_no_skill_path_resolver(tools: Sequence[BaseTool]) -> None:
    """`L3-IMP-14`: asserted on the bound toolset itself, never on the
    model's account of what it did -- the same reason `L3-REV-2`'s
    read-only reviewers are a toolset fact rather than an instruction.
    Raises `SkillPathResolverPresent`, naming every offending tool, where
    one is found."""
    offenders = sorted(tool.name for tool in tools if _resolves_a_skill_path(tool))
    if offenders:
        raise SkillPathResolverPresent(
            "tool(s) can resolve an arbitrary path under a skill's directory, "
            f"which ADR 0012 requires none can: {', '.join(offenders)}"
        )
