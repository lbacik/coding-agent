from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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

from coding_agent.runtime.context_handoff import ContextWindowConfig

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


# --- MVP step opening (contract §8) ---------------------------------------
#
# Contract: ``docs/contract/mvp-runtime-contract.md`` §8 (Opening a step, The
# Attempt Header, Tools).
#
# MVP row ids: C-SKL-2, G-HDR-1, L3-IMP-1, L3-IMP-15, P-CTX-1.
#
# The S3 eager-injection composition above (``compose_pinned_prefix``,
# ``InjectedFile``) is the pre-MVP arrangement ADR 0011's narrowing (#60,
# #61) superseded: a skill body now reaches the model only through its own
# activation call, as an ordinary Exchange Unit, never as a Pinned Prefix
# member. New Attempt code uses the header below plus the Target Issue's
# title and body; nothing here reads a skill file.

import re as _re

_HEADING_RE = _re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_DIRECT_CHECKLIST_RE = _re.compile(r"^ {0,3}[-*] +\[[ xX]\] +(.*\S)\s*$")


def enumerate_acceptance_criteria(body: str) -> tuple[str, ...]:
    """Enumerate Acceptance Criteria from a Target Issue body (contract §7).

    Recognises exactly one level-2 heading whose normalised text is
    ``Acceptance criteria`` (case-insensitive) and reads the non-empty
    direct checklist items beneath it, up to the next heading of the same
    or higher level. Each direct item, with its continued lines and nested
    content, is one criterion; nested checklist items never create criteria
    of their own. A checked box still declares a criterion and is never
    evidence. No section, more than one, or a section without direct
    checklist items yields no criteria.
    """
    lines = body.splitlines()
    sections: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        if level == 2 and match.group(2).strip().lower() == "acceptance criteria":
            sections.append((index, level))
    if len(sections) != 1:
        return ()
    (heading_index, _) = sections[0]
    end = len(lines)
    for index in range(heading_index + 1, len(lines)):
        match = _HEADING_RE.match(lines[index])
        if match is not None and len(match.group(1)) <= 2:
            end = index
            break
    criteria: list[str] = []
    current: list[str] | None = None
    for line in lines[heading_index + 1 : end]:
        item = _DIRECT_CHECKLIST_RE.match(line)
        if item is not None:
            if current is not None:
                text = "\n".join(current).strip()
                if text:
                    criteria.append(text)
            current = [item.group(1).strip()]
        elif current is not None:
            current.append(line)
    if current is not None:
        text = "\n".join(current).strip()
        if text:
            criteria.append(text)
    return tuple(criteria)


def compose_mvp_attempt_header(
    *,
    attempt_id: str,
    base_revision: str,
    branch: str,
    acceptance_criteria: tuple[tuple[str, str], ...],
    seam_set: tuple[str, ...],
    target_language: str,
    checks: tuple[str, ...] = (),
) -> str:
    """Author the Attempt Header for one step's first request (contract §8).

    Carries the Attempt id, the Base Revision and the branch; the
    Acceptance Criteria with their ids and the Seam Set; the statement
    that tests run by any channel other than the runtime-owned
    targeted-test tool count for nothing and only that tool produces
    Criterion Evidence; the role of ``run_command`` (a general shell in
    the working directory, credential-free, with the code-owned timeout),
    that nothing run through it counts as evidence, and that commits, the
    branch and HEAD are the runtime's; the Validation Contract's declared
    checks with the diagnostic rule for ``run_check`` (diagnostic only;
    ``final_checks`` decides, with no remediation round — or, under
    ``checks: none``, that no checks are declared and ``run_check`` is
    unavailable); an
    answer to every slash-command reference the activatable skills make
    (``/tdd`` → ``activate_tdd``, ``/code-review`` →
    ``activate_code_review``, ``/codebase-design`` →
    ``activate_codebase-design``); the Review rule with the five answers to
    ``code-review``'s references; and the two upstream-text answers of
    L3-IMP-15. The answers never edit the skill text (ADR 0007).

    MVP row ids: G-HDR-1, L3-IMP-15, C-RUN-6, C-CHK-9.
    """
    if acceptance_criteria:
        criteria_lines = "\n".join(f"- {cid}: {text}" for cid, text in acceptance_criteria)
    else:
        criteria_lines = "- (none)"
    if seam_set:
        seam_text = ", ".join(f"`{seam}`" for seam in seam_set)
    else:
        seam_text = "(none)"
    if checks:
        checks_text = ", ".join(f"`{check}`" for check in checks)
        checks_paragraph = (
            f"Checks ({len(checks)} declared): {checks_text}. `run_check` runs "
            "one named check exactly as pinned, for diagnosis only: its result "
            "is not evidence and changes no outcome. `final_checks` runs every "
            "check once against the Delivery Snapshot and decides, with no "
            "remediation round."
        )
    else:
        checks_paragraph = (
            "Checks: none declared. `run_check` is unavailable: no check may "
            "run in the tool loop."
        )
    return (
        "# Attempt Header\n\n"
        f"Attempt: {attempt_id}\n"
        f"Base Revision: {base_revision}\n"
        f"Branch: {branch}\n"
        f"Target Project language: {target_language}\n\n"
        "Acceptance Criteria (the runtime's enumeration, never the model's):\n"
        f"{criteria_lines}\n\n"
        "Seam Set (ADR 0008): the public boundaries your tests may be written "
        f"at, already confirmed before this conversation opened -- {seam_text}. "
        "Never re-derive or renegotiate it.\n\n"
        "Evidence rule: tests run by any channel other than `test_targeted` "
        "count for nothing — including tests run through `run_command`. "
        "Only the runtime-owned targeted-test tool (`test_targeted`) produces "
        "Criterion Evidence. A claim naming a test that Criterion Evidence "
        "does not record as passing is returned. "
        "`run_command` is a general shell in the working directory: it runs "
        "a command string through `sh -c` with no credential in its "
        "environment and the code-owned per-command timeout (a shorter "
        "timeout may be requested, never a longer one), and returns the exit "
        "code, stdout and stderr. Nothing run through `run_command` counts "
        "as evidence. Commits, the branch and HEAD are the runtime's: the "
        "model never commits, and a moved branch or HEAD is restored.\n\n"
        f"{checks_paragraph}\n\n"
        "Slash-command answers (the skills below are activated by tool call, "
        "never by slash command):\n"
        "- `/tdd` → call the `activate_tdd` tool.\n"
        "- `/code-review` → call the `activate_code_review` tool.\n"
        "- `/codebase-design` → call the `activate_codebase-design` tool.\n\n"
        "Review rule: `activate_code_review` records the review snapshot X "
        "and returns the `code-review` skill body, byte-identical. You run "
        "the review yourself: the Review is that activation plus the first "
        "`spawn_subagent` group after it, in the same work node, on a tree "
        "equal to X. Five answers to that skill's references, here rather "
        "than edited into its text (ADR 0007):\n"
        "- sub-agents → one `spawn_subagent` call per Sub-agent, as separate "
        "calls in one turn (one group);\n"
        "- fixed point → the Base Revision above; no user is involved;\n"
        "- diff → the `read_change` tool, bound in this step and in every "
        "Sub-agent (what the Delivery Snapshot would commit: the working "
        "tree including untracked files, with the same exclusions);\n"
        "- spec → the Target Issue below, passed in `instructions`;\n"
        "- aggregate → the pull request renders your reports from the Run "
        "Ledger; you do not compose the pull request.\n\n"
        "Two things the upstream skill files refer to but do not themselves "
        "contain, answered here rather than edited into their text "
        "(ADR 0007):\n\n"
        "- `codebase-design`'s own \"Going deeper\" companion files "
        "(`DEEPENING.md`, `DESIGN-IT-TWICE.md`) are unavailable in this "
        "Attempt. Its `SKILL.md`, when activated, is the whole of what you "
        "have of it.\n"
        "- `tdd`'s companion files illustrate every example in TypeScript "
        "and jest. The principles in both files apply regardless of "
        "language, and the syntax does not -- translate the pattern, not "
        f"the syntax, to {target_language}.\n\n"
        "Use `find_files` or `search_text` before broad directory walking or "
        "whole-file reads. Once a region is known, use `read_lines` with its "
        "stable one-based references."
    )


def build_step_opening_messages(
    attempt_header: str, issue_title: str, issue_body: str
) -> list[Any]:
    """Open a step: the Pinned Prefix as its own messages (contract §8).

    The Attempt Header first, then the Target Issue's title and body; no
    comments, no skill body. Skill bodies arrive only as activation
    results. Typed as ``list[Any]`` so this module stays free of the
    message-class import its single caller already owns.
    """
    from langchain_core.messages import HumanMessage as _HumanMessage
    from langchain_core.messages import SystemMessage as _SystemMessage

    return [
        _SystemMessage(content=attempt_header),
        _HumanMessage(content=f"# Target Issue\n\n{issue_title}\n\n{issue_body}"),
    ]
