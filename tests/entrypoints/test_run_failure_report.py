"""The headless CLI tells the operator why a run stopped, from the typed failure core emits."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from entrypoints.cli.loops import _describe_failure
from vibesys.api import RunFailure, RunFailureKind


@given(
    kind=st.sampled_from(RunFailureKind),
    started=st.integers(min_value=0, max_value=50),
    extra=st.integers(min_value=0, max_value=50),
    kept=st.integers(min_value=0, max_value=50),
    reason=st.text(alphabet="abcdefg ", min_size=1, max_size=40),
)
def test_every_failure_kind_prints_its_counts_and_the_cores_account(
    kind: RunFailureKind, started: int, extra: int, kept: int, reason: str
) -> None:
    failure = RunFailure(
        kind=kind,
        reason=reason,
        workstreams_started=started,
        workstream_budget=started + extra,
        candidates_kept=kept,
    )

    line = _describe_failure(failure)

    assert "\n" not in line
    assert line.startswith("Reason: ")
    assert f"{started} of {started + extra} workstreams started" in line
    assert f"{kept} candidates kept" in line
    assert line.endswith(reason)


def test_each_kind_reads_differently() -> None:
    lines = {
        _describe_failure(
            RunFailure(
                kind=kind,
                reason="r",
                workstreams_started=1,
                workstream_budget=2,
                candidates_kept=0,
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
                "extra": 1,
            }
        )
