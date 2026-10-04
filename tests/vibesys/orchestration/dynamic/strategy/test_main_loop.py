"""The main loop end to end against a fake core: baseline, plan, implement, measure, adopt."""

from __future__ import annotations

from collections import deque

from tests.vibesys.orchestration.dynamic.strategy._fake_core import (
    FakeCore,
    Script,
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.strategy.api import DynamicConfig, DynamicStrategy
from vs_core.api import ArtifactId, ArtifactRef, Measure, ProposeWinner, StartAttempt, Stop


def _config(**overrides: object) -> DynamicConfig:
    return DynamicConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 1,
            "max_in_flight": 1,
            **overrides,
        }
    )


def test_single_hypothesis_runs_to_adoption() -> None:
    script = Script(
        planner=deque([plan_reply(implement("h1"))]),
        implementer=deque([implemented()]),
        judge=deque([reviewed()]),
    )
    core = FakeCore(strategy=DynamicStrategy(config=_config()), script=script)
    core.run()
    kinds = [type(item).__name__ for item in core.decisions]
    assert kinds.count("StartAttempt") == 1
    assert isinstance(core.decisions[-1], Stop)
    assert core.decisions[-1].result.outcome == "success"
    proposal = next(item for item in core.decisions if isinstance(item, ProposeWinner))
    assert proposal.selection.kind == "retained_candidate"
    assert any(isinstance(item, Measure) for item in core.decisions)
    assert any(isinstance(item, StartAttempt) for item in core.decisions)
