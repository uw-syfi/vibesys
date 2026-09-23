"""Agent-specific server readers ignore another policy's run projection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.api.design import DesignLog
from server.api.experiments import build_experiment_log
from server.api.performance import build_performance_context
from vibesys.api import RunStatus, RunView

if TYPE_CHECKING:
    from pathlib import Path


def test_agent_readers_ignore_a_custom_projection(tmp_path: Path) -> None:
    view = RunView(
        run_id="team-run",
        loop="team-search",
        status=RunStatus.UNKNOWN,
        projection={"kind": "team-search", "workers": 3},
    )
    design = DesignLog(
        workspace=tmp_path,
        diff=lambda _base, _head: "",
        patch=lambda _base, _head, _paths: "",
    )

    assert build_experiment_log(view) == []
    assert build_performance_context(view, objectives=()) is None
    assert design.rounds(view, baseline="0" * 40) == []
