"""Tests for typed round records, their codec, and rollback resolution."""

import pytest
from pydantic import ValidationError

from vs_loop_state.api import (
    JudgeVerdict,
    RoundHistory,
    RoundRecord,
    parse_round_record,
    serialize_round_record,
)


def _record(
    round_number: int,
    commit: str | None,
    *,
    outcome: str | None = None,
    parent_round: int | None = None,
    parent_commit: str | None = None,
) -> RoundRecord:
    return RoundRecord(
        round_number=round_number,
        commit=commit,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_outcome=outcome,
        hypothesis_parent_round=parent_round,
        hypothesis_parent_commit=parent_commit,
    )


def test_public_round_record_codec_round_trips_current_schema() -> None:
    record = RoundRecord(
        round_number=3,
        commit="a" * 40,
        perf_metric=1.0,
        perf_unit="ops/s",
        passed=True,
        hypothesis_declared_outcome="nominated",
        judge_verdict="pass",
        official_evaluation=True,
        official_evaluation_reason="cadence",
        metrics={"throughput": 1.0},
        candidate_disposition="retained",
        candidate_metrics={"latency": 2.0},
        candidate_retained=True,
        perf_direction="max",
        perf_baseline_round=2,
        perf_baseline_commit="b" * 40,
        perf_baseline_metric=0.9,
        perf_delta_pct=100 / 9,
    )

    payload = serialize_round_record(record)

    assert payload["round"] == 3
    assert "round_number" not in payload
    assert parse_round_record(payload) == record


def test_implementer_attribution_round_trips_and_defaults_to_none() -> None:
    bare = _record(1, "a" * 40)
    assert bare.implementer_driver is None
    assert bare.implementer_provider is None
    assert bare.implementer_model is None

    attributed = RoundRecord(
        round_number=2,
        commit="c" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        implementer_driver="agentshim",
        implementer_provider="codex",
        implementer_model="gpt-5.6-sol",
    )
    payload = serialize_round_record(attributed)
    assert payload["implementer_provider"] == "codex"
    assert payload["implementer_model"] == "gpt-5.6-sol"
    assert parse_round_record(payload) == attributed


def test_legacy_record_without_attribution_still_loads() -> None:
    # A record written before attribution existed has none of the new keys;
    # ``extra="forbid"`` rejects unknown keys, not missing defaulted ones.
    legacy = serialize_round_record(_record(1, "a" * 40))
    for key in ("implementer_driver", "implementer_provider", "implementer_model"):
        legacy.pop(key, None)
    restored = parse_round_record(legacy)
    assert restored.implementer_provider is None
    assert restored.implementer_driver is None


def test_parse_round_record_applies_declared_defaults() -> None:
    record = parse_round_record(
        {
            "round": 1,
            "commit": None,
            "perf_metric": None,
            "perf_unit": None,
            "passed": False,
        }
    )

    assert record.profile_skipped is False
    assert record.reviewed is True
    assert record.official_evaluation is False
    assert record.candidate_disposition == "unassessed"
    assert record.hypothesis_declared_outcome is None
    assert record.judge_verdict is None
    assert record.candidate_retained is None
    assert record.perf_direction is None


def test_parse_round_record_preserves_explicit_official_evaluation() -> None:
    record = parse_round_record(
        {
            "round": 1,
            "commit": "a" * 40,
            "perf_metric": None,
            "perf_unit": None,
            "passed": True,
            "hypothesis_outcome": "proven",
            "official_evaluation": False,
        }
    )

    assert record.official_evaluation is False


def test_parse_round_record_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        parse_round_record(
            {
                "round": 1,
                "commit": None,
                "perf_metric": None,
                "perf_unit": None,
                "passed": False,
                "made_up_field": "surprise",
            }
        )


def _judged_record(round_number: int, verdict: JudgeVerdict) -> RoundRecord:
    return RoundRecord(
        round_number=round_number,
        commit=f"{round_number:040x}",
        perf_metric=None,
        perf_unit=None,
        passed=verdict == "pass",
        judge_verdict=verdict,
    )


def test_review_state_is_derived_from_the_verdict() -> None:
    """``reviewed`` restates the verdict, so only the verdict is stored."""
    assert _judged_record(1, "pass").reviewed is True
    assert _judged_record(2, "fail").reviewed is True
    assert _judged_record(3, "deferred").reviewed is False


def test_serialized_record_keeps_the_reviewed_key() -> None:
    payload = serialize_round_record(_judged_record(4, "deferred"))

    assert payload["reviewed"] is False
    assert payload["judge_verdict"] == "deferred"
    assert parse_round_record(payload).reviewed is False


def test_legacy_record_without_a_verdict_keeps_its_reviewed_flag() -> None:
    """Records written before ``judge_verdict`` existed still load unchanged."""
    reviewed = parse_round_record(
        {"round": 1, "commit": None, "perf_metric": None, "perf_unit": None, "passed": True}
    )
    unreviewed = parse_round_record(
        {
            "round": 2,
            "commit": None,
            "perf_metric": None,
            "perf_unit": None,
            "passed": False,
            "reviewed": False,
        }
    )

    assert reviewed.judge_verdict is None
    assert reviewed.reviewed is True
    assert unreviewed.judge_verdict is None
    assert unreviewed.reviewed is False
    assert serialize_round_record(unreviewed)["reviewed"] is False


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_unreviewed_record_with_a_verdict_normalizes_to_deferred(verdict: str) -> None:
    """Repairs runs corrupted by the stale-verdict bug (issue #503).

    Such a record can only have taken its verdict from an earlier implementer
    attempt of the same round, so it must reproject as unreviewed instead of
    resolving the hypothesis on evidence that describes a different attempt.
    """
    record = parse_round_record(
        {
            "round": 3,
            "commit": "c" * 40,
            "perf_metric": None,
            "perf_unit": None,
            "passed": False,
            "reviewed": False,
            "judge_verdict": verdict,
            "hypothesis_outcome": "continue",
        }
    )

    assert record.judge_verdict == "deferred"
    assert record.reviewed is False
    assert serialize_round_record(record)["judge_verdict"] == "deferred"


def test_constructing_an_unreviewed_verdict_normalizes_it_too() -> None:
    """The invariant holds for in-memory construction, not only for loads."""
    record = RoundRecord(
        round_number=4,
        commit="d" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=False,
        reviewed=False,
        judge_verdict="pass",
    )

    assert record.judge_verdict == "deferred"
    assert record.reviewed is False


def test_round_history_starts_empty_and_appends_records() -> None:
    history = RoundHistory()
    first = _record(1, "a" * 40)
    second = _record(2, "b" * 40)

    history.append(first)
    history.append(second)

    assert history.records == [first, second]


def test_rollback_with_no_history_returns_target_commit_unchanged() -> None:
    target = _record(5, "a" * 40)

    commit, child_round = RoundHistory().resolve_rollback_commit(target, {"disproven"})

    assert commit == "a" * 40
    assert child_round is None


@pytest.mark.parametrize("failed_outcome", ["disproven", "implementation_failed"])
def test_failed_latest_child_rollback_uses_its_exact_parent_commit(
    failed_outcome: str,
) -> None:
    historical_parent = _record(20, "a" * 40)
    failed_child = _record(
        21,
        "c" * 40,
        outcome=failed_outcome,
        parent_round=20,
        parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_parent, failed_child])

    commit, child_round = history.resolve_rollback_commit(
        historical_parent,
        {"disproven", "implementation_failed"},
    )

    assert commit == "b" * 40
    assert child_round == 21


def test_rollback_ignores_outcomes_outside_the_failure_set() -> None:
    historical_parent = _record(20, "a" * 40)
    continuing_child = _record(
        21,
        "c" * 40,
        outcome="continue",
        parent_round=20,
        parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_parent, continuing_child])

    commit, child_round = history.resolve_rollback_commit(
        historical_parent,
        {"disproven", "implementation_failed"},
    )

    assert commit == "a" * 40
    assert child_round is None


def test_rollback_requires_the_failed_child_to_be_latest() -> None:
    historical_parent = _record(20, "a" * 40)
    failed_child = _record(
        21,
        "c" * 40,
        outcome="disproven",
        parent_round=20,
        parent_commit="b" * 40,
    )
    later_round = _record(22, "d" * 40)
    history = RoundHistory(records=[historical_parent, failed_child, later_round])

    commit, child_round = history.resolve_rollback_commit(historical_parent, {"disproven"})

    assert commit == "a" * 40
    assert child_round is None


def test_rollback_requires_the_failed_childs_parent_commit() -> None:
    historical_parent = _record(20, "a" * 40)
    failed_child = _record(
        21,
        "c" * 40,
        outcome="disproven",
        parent_round=20,
    )
    history = RoundHistory(records=[historical_parent, failed_child])

    commit, child_round = history.resolve_rollback_commit(historical_parent, {"disproven"})

    assert commit == "a" * 40
    assert child_round is None


def test_distant_rollback_uses_requested_historical_commit() -> None:
    historical_target = _record(5, "a" * 40)
    failed_child = _record(
        21,
        "c" * 40,
        outcome="disproven",
        parent_round=20,
        parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_target, failed_child])

    commit, child_round = history.resolve_rollback_commit(historical_target, {"disproven"})

    assert commit == "a" * 40
    assert child_round is None


def test_hypothesis_plan_text_round_trips() -> None:
    """The claim and attempted change must survive the round-record codec.

    ``active_hypothesis.json`` only holds the live plan, so a resolved
    hypothesis would otherwise lose the text the experiment log displays.
    """
    record = RoundRecord(
        round_number=7,
        commit="a" * 40,
        perf_metric=112.0,
        perf_unit="tok_s",
        passed=True,
        hypothesis_id="H-07",
        hypothesis_outcome="proven",
        hypothesis_claim="batching prefill removes per-request launch overhead",
        hypothesis_task="batch the prefill step",
    )

    reloaded = parse_round_record(serialize_round_record(record))

    assert reloaded.hypothesis_claim == "batching prefill removes per-request launch overhead"
    assert reloaded.hypothesis_task == "batch the prefill step"


def test_record_written_before_hypothesis_plan_text_loads() -> None:
    reloaded = parse_round_record(
        {
            "round": 3,
            "commit": "b" * 40,
            "perf_metric": None,
            "perf_unit": None,
            "passed": False,
            "hypothesis_id": "H-03",
        }
    )

    assert reloaded.hypothesis_claim is None
    assert reloaded.hypothesis_task is None
