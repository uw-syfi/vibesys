"""Tests for the agent loop's per-attempt judge outcome."""

from __future__ import annotations

import dataclasses
from typing import Literal

import pytest

from vibesys.search.hypothesis.attempts import (
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    attempt_was_reviewed,
    recorded_judge_verdict,
)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("pass", "pass"), ("fail", "fail")],
)
def test_reviewed_attempt_records_its_verdict(
    verdict: Literal["pass", "fail"], expected: str
) -> None:
    outcome = JudgeReviewed(verdict)

    assert attempt_was_reviewed(outcome) is True
    assert recorded_judge_verdict(outcome) == expected


@pytest.mark.parametrize("reason", list(JudgeSkipReason))
def test_skipped_attempt_records_deferred_whatever_the_reason(reason: JudgeSkipReason) -> None:
    """Every skip is unreviewed and deferred; the reason is diagnostic only."""
    outcome = JudgeSkipped(reason)

    assert attempt_was_reviewed(outcome) is False
    assert recorded_judge_verdict(outcome) == "deferred"


def test_a_skipped_attempt_cannot_carry_a_verdict() -> None:
    """The two variants are disjoint, so 'verdict without review' has no value."""
    assert not hasattr(JudgeSkipped(JudgeSkipReason.NOT_REACHED), "verdict")
    assert not hasattr(JudgeReviewed("pass"), "reason")


def test_an_attempt_outcome_is_immutable() -> None:
    """An attempt's result is replaced by the next attempt, never updated."""
    outcome = JudgeReviewed("pass")
    attribute = "verdict"

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(outcome, attribute, "fail")
