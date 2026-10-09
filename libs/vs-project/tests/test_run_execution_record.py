"""The persisted execution record accepts only runs AgentShim could have driven."""

from __future__ import annotations

import pytest
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
    record = RunExecutionRecord.model_validate(_record(agent_driver=recorded))

    assert record.agent_driver == recorded


@pytest.mark.parametrize("recorded", ["omnigent", "unknown", ""])
def test_a_run_that_recorded_a_removed_driver_is_rejected_naming_the_key(recorded: str) -> None:
    with pytest.raises(ValidationError, match="agent_driver"):
        RunExecutionRecord.model_validate(_record(agent_driver=recorded))
