"""Tests for RoundRecord validation and RoundHistory persistence/rollback resolution."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vs_loop_state.agent import RoundHistory, RoundRecord

# ---------------------------------------------------------------------------
# On-disk shape
# ---------------------------------------------------------------------------


def test_persisted_round_uses_the_round_key_not_round_number(tmp_path: Path) -> None:
    path = tmp_path / "rounds.json"
    history = RoundHistory(path)
    history.append(
        RoundRecord(round_number=3, commit="a" * 40, perf_metric=None, perf_unit=None, passed=True)
    )

    history.save()

    raw = json.loads(path.read_text())
    assert raw[0]["round"] == 3
    assert "round_number" not in raw[0]


def test_load_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "rounds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "round": 1,
                    "commit": None,
                    "perf_metric": None,
                    "perf_unit": None,
                    "passed": False,
                    "made_up_field": "surprise",
                }
            ]
        )
    )

    with pytest.raises(ValidationError):
        RoundHistory.load(path)


def test_legacy_record_without_official_evaluation_infers_from_proven_outcome(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rounds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "round": 1,
                    "commit": "a" * 40,
                    "perf_metric": 10.0,
                    "perf_unit": "tok/s",
                    "passed": True,
                    "hypothesis_outcome": "proven",
                },
                {
                    "round": 2,
                    "commit": "b" * 40,
                    "perf_metric": None,
                    "perf_unit": None,
                    "passed": False,
                    "hypothesis_outcome": "disproven",
                },
            ]
        )
    )

    history = RoundHistory.load(path)

    assert history.records[0].official_evaluation is True
    assert history.records[1].official_evaluation is False


def test_explicit_official_evaluation_is_not_overridden(tmp_path: Path) -> None:
    path = tmp_path / "rounds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "round": 1,
                    "commit": "a" * 40,
                    "perf_metric": None,
                    "perf_unit": None,
                    "passed": True,
                    "hypothesis_outcome": "proven",
                    "official_evaluation": False,
                }
            ]
        )
    )

    history = RoundHistory.load(path)

    assert history.records[0].official_evaluation is False


# ---------------------------------------------------------------------------
# RoundHistory persistence
# ---------------------------------------------------------------------------


def test_load_missing_file_starts_empty(tmp_path: Path) -> None:
    assert RoundHistory.load(tmp_path / "rounds.json").records == []


def test_save_and_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "rounds.json"
    history = RoundHistory(path)
    history.append(
        RoundRecord(
            round_number=1, commit="a" * 40, perf_metric=1.0, perf_unit="tok/s", passed=True
        )
    )
    history.append(
        RoundRecord(round_number=2, commit="b" * 40, perf_metric=None, perf_unit=None, passed=False)
    )

    history.save()
    reloaded = RoundHistory.load(path)

    assert reloaded.records == history.records


def test_save_without_a_path_raises() -> None:
    history = RoundHistory(records=[])

    with pytest.raises(ValueError, match="no path to save to"):
        history.save()


# ---------------------------------------------------------------------------
# Rollback resolution
# ---------------------------------------------------------------------------


def test_rollback_with_no_history_returns_target_commit_unchanged() -> None:
    target = RoundRecord(
        round_number=5, commit="a" * 40, perf_metric=None, perf_unit=None, passed=True
    )
    history = RoundHistory(records=[])

    commit, child_round = history.resolve_rollback_commit(target, {"disproven"})

    assert commit == "a" * 40
    assert child_round is None


def test_failed_child_rollback_preserves_its_exact_parent_commit() -> None:
    historical_parent = RoundRecord(
        round_number=20, commit="a" * 40, perf_metric=1400.0, perf_unit="tok/s", passed=True
    )
    failed_child = RoundRecord(
        round_number=21,
        commit="c" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_outcome="disproven",
        hypothesis_parent_round=20,
        hypothesis_parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_parent, failed_child])

    commit, child_round = history.resolve_rollback_commit(historical_parent, {"disproven"})

    assert commit == "b" * 40
    assert child_round == 21


def test_implementation_failed_child_rollback_preserves_its_exact_parent_commit() -> None:
    historical_parent = RoundRecord(
        round_number=20, commit="a" * 40, perf_metric=1400.0, perf_unit="tok/s", passed=True
    )
    failed_child = RoundRecord(
        round_number=21,
        commit="c" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_outcome="implementation_failed",
        hypothesis_parent_round=20,
        hypothesis_parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_parent, failed_child])

    commit, child_round = history.resolve_rollback_commit(
        historical_parent, {"implementation_failed"}
    )

    assert commit == "b" * 40
    assert child_round == 21


def test_rollback_ignores_outcomes_outside_the_caller_supplied_failure_set() -> None:
    historical_parent = RoundRecord(
        round_number=20, commit="a" * 40, perf_metric=1400.0, perf_unit="tok/s", passed=True
    )
    continuing_child = RoundRecord(
        round_number=21,
        commit="c" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_outcome="continue",
        hypothesis_parent_round=20,
        hypothesis_parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_parent, continuing_child])

    commit, child_round = history.resolve_rollback_commit(
        historical_parent, {"disproven", "implementation_failed"}
    )

    assert commit == "a" * 40
    assert child_round is None


def test_distant_rollback_uses_requested_historical_commit() -> None:
    historical_parent = RoundRecord(
        round_number=5, commit="a" * 40, perf_metric=None, perf_unit=None, passed=True
    )
    failed_child = RoundRecord(
        round_number=21,
        commit="c" * 40,
        perf_metric=None,
        perf_unit=None,
        passed=True,
        hypothesis_outcome="disproven",
        hypothesis_parent_round=20,
        hypothesis_parent_commit="b" * 40,
    )
    history = RoundHistory(records=[historical_parent, failed_child])

    commit, child_round = history.resolve_rollback_commit(historical_parent, {"disproven"})

    assert commit == "a" * 40
    assert child_round is None
