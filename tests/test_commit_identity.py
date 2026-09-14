from __future__ import annotations

from coding_agent.identity.commit_identity import (
    attempt_branch_name,
    attempt_trailer,
    delivery_snapshot_message,
    noreply_author,
    slugify,
)
from coding_agent.identity.startup import Identity


def test_noreply_author_is_id_based() -> None:
    identity = Identity(account_id=12345, login="coding-agent")
    name, email = noreply_author(identity)
    assert name == "coding-agent"
    assert email == "12345+coding-agent@users.noreply.github.com"


def test_slugify_collapses_and_lowercases() -> None:
    assert slugify("Fix the Thing!! (again)") == "fix-the-thing-again"


def test_slugify_trims_to_max_length_on_a_word_boundary() -> None:
    slug = slugify("one two three four five six seven eight nine ten", max_length=20)
    assert len(slug) <= 20
    assert not slug.endswith("-")


def test_attempt_branch_name() -> None:
    assert attempt_branch_name(34, 1, "Fix the loop") == "agent/34/1-fix-the-loop"


def test_attempt_trailer() -> None:
    assert attempt_trailer(34, 2) == "Attempt: #34/2"


def test_delivery_snapshot_message_carries_the_trailer() -> None:
    message = delivery_snapshot_message(34, "Fix the loop", 1)
    assert message.startswith("Implement #34: Fix the loop\n\n")
    assert message.endswith("Attempt: #34/1")
