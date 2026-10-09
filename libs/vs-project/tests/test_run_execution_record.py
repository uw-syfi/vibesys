"""The persisted execution record loads runs that recorded the retired agent_driver key."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_project.api import RunExecutionRecord


def _record(**extra: object) -> dict[str, object]:
    return {
        "model": "m",
        "agent_backend": "cli",
        "compute_backend": "cpu",
        "requested_profiler": "none",
        "resolved_profiler": "none",
        "agent_roles": {},
        **extra,
    }


@pytest.mark.parametrize("recorded", [None, "agentshim"])
def test_runs_that_recorded_agentshim_or_nothing_still_load(recorded: str | None) -> None:
    legacy = RunExecutionRecord.model_validate(_record(agent_driver=recorded))

    assert legacy == RunExecutionRecord.model_validate(_record())
    assert "agent_driver" not in legacy.model_dump()


@pytest.mark.parametrize("recorded", ["omnigent", "unknown", ""])
def test_a_run_that_recorded_a_removed_driver_is_rejected_naming_the_key(recorded: str) -> None:
    with pytest.raises(ValidationError, match="agent_driver"):
        RunExecutionRecord.model_validate(_record(agent_driver=recorded))


@given(key=st.text(min_size=1).filter(lambda k: k not in _record() and k != "agent_driver"))
def test_any_other_unknown_key_is_still_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        RunExecutionRecord.model_validate(_record(**{key: 1}))
