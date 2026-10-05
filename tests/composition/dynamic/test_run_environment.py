"""A run environment that cannot open candidate sandboxes is refused before any agent turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.composition.dynamic._harness import LoopInput, ScriptedAgents, run_request

from vibesys.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path


def test_the_local_environment_is_refused_before_any_agent_turn(tmp_path: Path) -> None:
    loop_input = LoopInput.create(tmp_path / "input")
    agents = ScriptedAgents()

    run = run_request(loop_input.request(run_environment="local"), agents)

    assert isinstance(run.error, ConfigurationError)
    message = str(run.error)
    assert "'local' run environment" in message
    assert "isolated candidate sandboxes" in message
    assert agents.turns == []
    assert loop_input.sbatch_count() == 0
