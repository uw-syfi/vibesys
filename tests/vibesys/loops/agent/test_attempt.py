"""Tests for the agent loop's per-attempt judge outcome."""

from __future__ import annotations

import dataclasses

import pytest

from vibesys.loops.agent.attempt import (
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    attempt_was_reviewed,
    recorded_judge_verdict,
)
from vibesys.schemas import Verdict


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [(Verdict.PASS, "pass"), (Verdict.FAIL, "fail")],
)
def test_reviewed_attempt_records_its_verdict(verdict: Verdict, expected: str) -> None:
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
    assert not hasattr(JudgeReviewed(Verdict.PASS), "reason")


def test_an_attempt_outcome_is_immutable() -> None:
    """An attempt's result is replaced by the next attempt, never updated."""
    outcome = JudgeReviewed(Verdict.PASS)
    attribute = "verdict"

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(outcome, attribute, Verdict.FAIL)
