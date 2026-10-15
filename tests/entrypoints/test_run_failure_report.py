"""The headless CLI tells the operator why a run stopped, from the typed failure core emits."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from entrypoints.cli.loops import describe_failure
from vibesys.api import RunFailure, RunFailureKind

ADOPTED_CANDIDATES = st.text(alphabet="0123456789abcdef", min_size=1, max_size=40)
VALID_FAILURE_ADOPTION = st.one_of(
    st.tuples(
        st.sampled_from(RunFailureKind),
        st.integers(min_value=0, max_value=50),
        st.none(),
    ),
    st.tuples(
        st.sampled_from([RunFailureKind.BUDGET_EXHAUSTED, RunFailureKind.NO_RESULT]),
        st.integers(min_value=1, max_value=50),
        ADOPTED_CANDIDATES,
    ),
)


@given(
    failure_adoption=VALID_FAILURE_ADOPTION,
    counts=st.tuples(
        st.integers(min_value=0, max_value=50), st.integers(min_value=0, max_value=50)
    ),
    reason=st.text(alphabet="abcdefg ", min_size=1, max_size=40),
)
def test_every_failure_kind_prints_its_counts_and_the_cores_account(
    failure_adoption: tuple[RunFailureKind, int, str | None],
    counts: tuple[int, int],
    reason: str,
) -> None:
    kind, kept, adopted_candidate = failure_adoption
    started, extra = counts
    failure = RunFailure(
        kind=kind,
        reason=reason,
        workstreams_started=started,
        workstream_budget=started + extra,
        candidates_kept=kept,
        adopted_candidate=adopted_candidate,
    )

    line = describe_failure(failure)

    assert "\n" not in line
    assert line.startswith("Reason: ")
    assert f"{started} of {started + extra} workstreams started" in line
    assert f"{kept} candidates kept" in line
    if adopted_candidate is not None:
        assert f"after adopting candidate {adopted_candidate}" in line
    assert line.endswith(reason)


def test_each_kind_reads_differently() -> None:
    lines = {
        describe_failure(
            RunFailure(
                kind=kind,
                reason="r",
                workstreams_started=1,
                workstream_budget=2,
                candidates_kept=0,
                adopted_candidate=None,
            )
        )
        for kind in RunFailureKind
    }
    assert len(lines) == len(RunFailureKind)


@pytest.mark.parametrize("kind", list(RunFailureKind))
def test_failure_models_reject_unknown_fields(kind: RunFailureKind) -> None:
    with pytest.raises(ValueError, match="extra"):
        RunFailure.model_validate(
            {
                "kind": kind,
                "reason": "r",
                "workstreams_started": 0,
                "workstream_budget": 0,
                "candidates_kept": 0,
                "adopted_candidate": None,
                "extra": 1,
            }
        )


@pytest.mark.parametrize("kind", list(RunFailureKind))
def test_a_recorded_failure_without_the_new_adoption_fact_remains_compatible(
    kind: RunFailureKind,
) -> None:
    failure = RunFailure.model_validate(
        {
            "kind": kind,
            "reason": "recorded by an older version",
            "workstreams_started": 1,
            "workstream_budget": 2,
            "candidates_kept": 1,
        }
    )

    assert failure.adopted_candidate is None
    assert "no candidate adopted" in describe_failure(failure)


@pytest.mark.parametrize("identity", ["", "not one identity"])
def test_an_adopted_candidate_requires_one_stable_identity(identity: str) -> None:
    with pytest.raises(ValueError, match="adopted_candidate"):
        RunFailure(
            kind=RunFailureKind.NO_RESULT,
            reason="r",
            workstreams_started=1,
            workstream_budget=2,
            candidates_kept=1,
            adopted_candidate=identity,
        )


@pytest.mark.parametrize(
    ("kind", "kept", "message"),
    [
        (RunFailureKind.BUDGET_EXHAUSTED, 0, "requires at least one candidate"),
        (RunFailureKind.DEADLINE, 1, "deadline failure cannot carry"),
    ],
)
def test_an_adopted_candidate_rejects_impossible_failure_states(
    kind: RunFailureKind, kept: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RunFailure(
            kind=kind,
            reason="r",
            workstreams_started=1,
            workstream_budget=2,
            candidates_kept=kept,
            adopted_candidate="abc123",
        )


@given(kept=st.integers(min_value=0, max_value=50), kind=st.sampled_from(RunFailureKind))
def test_the_headline_claims_no_result_only_when_nothing_was_kept(
    kept: int, kind: RunFailureKind
) -> None:
    line = describe_failure(
        RunFailure(
            kind=kind,
            reason="r",
            workstreams_started=1,
            workstream_budget=2,
            candidates_kept=kept,
            adopted_candidate=None,
        )
    )
    assert "a candidate" not in line
    assert ("no result to keep" in line) == (kept == 0)


def test_the_headline_reports_a_candidate_that_was_adopted() -> None:
    line = describe_failure(
        RunFailure(
            kind=RunFailureKind.BUDGET_EXHAUSTED,
            reason="out of rounds",
            workstreams_started=2,
            workstream_budget=2,
            candidates_kept=1,
            adopted_candidate="abc123",
        )
    )

    assert "after adopting candidate abc123" in line
    assert "no candidate adopted" not in line
