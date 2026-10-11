"""The policy two serial strategies share: plan acceptance, attribution and the plan view."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.hypothesis import HypothesisConfig, OrchestratorPlan, rounds
from vibesys.profile_focus import ProfileAttributionError
from vibesys.serial_search.api import (
    AttributionConfig,
    accept_plan,
    bottlenecks_of,
    focus_of,
    focus_policy,
    plan_evidence,
    record_round,
)

_CONFIG = HypothesisConfig(max_rounds=4)
_TEXT = st.text(alphabet="abcdefg hij", min_size=1, max_size=12)


def _reply(identifier: str, **extra: object) -> str:
    return json.dumps(
        {
            "hypothesis_id": identifier,
            "title": "a title",
            "hypothesis": "claim",
            "task": "task",
            "pass_criteria": "faster",
            "reasoning": "because",
            **extra,
        }
    )


@given(identifier=st.text(alphabet=" \tabcxyz-0123", max_size=12))
def test_a_valid_plan_gets_a_stripped_nonempty_id_whatever_the_designer_wrote(
    identifier: str,
) -> None:
    plan = accept_plan(
        _CONFIG,
        rounds.Ledger(rounds.decode(rounds.EncodedLedger()).search),
        _reply(identifier),
        round_number=3,
    )
    assert isinstance(plan, OrchestratorPlan)
    assert plan.hypothesis_id == (identifier.strip() or "hypothesis-0003")
    assert plan.recommended_skills == []


@given(text=_TEXT)
def test_a_reply_that_is_not_a_plan_is_refused_with_a_reason(text: str) -> None:
    refused = accept_plan(_CONFIG, rounds.decode(rounds.EncodedLedger()), text, round_number=1)
    assert isinstance(refused, str)
    assert refused


def test_a_plan_that_updates_itself_twice_or_reuses_an_id_is_refused() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    update = {"hypothesis_id": "old", "disposition": "parked", "reason": "defective"}
    duplicate = accept_plan(
        _CONFIG, ledger, _reply("h1", hypothesis_updates=[update, update]), round_number=1
    )
    selfish = accept_plan(
        _CONFIG,
        ledger,
        _reply("h1", hypothesis_updates=[{**update, "hypothesis_id": "h1"}]),
        round_number=1,
    )
    assert "more than once" in str(duplicate)
    assert "new hypothesis" in str(selfish)


def test_attribution_output_that_is_not_the_protocol_is_refused() -> None:
    with pytest.raises(ProfileAttributionError):
        bottlenecks_of("not json")


def test_a_plain_run_has_no_focus_policy_and_records_nothing() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    assert focus_policy(None) is None
    assert focus_of(ledger, None) is None
    assert record_round(ledger, None, passed=True) is ledger


def test_a_profile_guided_run_has_a_policy_from_its_thresholds() -> None:
    policy = focus_policy(AttributionConfig(command=("profile",), min_measured_rounds=3))
    assert policy is not None
    assert focus_of(rounds.decode(rounds.EncodedLedger()), policy) is None


def test_the_plan_view_of_a_new_run_shows_no_history() -> None:
    ledger = rounds.decode(rounds.EncodedLedger())
    planning = rounds.planning(_CONFIG, ledger, 1)
    evidence = plan_evidence(_CONFIG, ledger, planning, None)
    assert (evidence.round_number, evidence.max_rounds) == (1, 4)
    assert evidence.history == ()
    assert evidence.frontier == ()
    assert evidence.older_rounds == 0
