"""A run environment that cannot open candidate sandboxes is refused before any agent turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.composition.dynamic._harness import LoopInput, ScriptedAgents, run_request
from tests.support.docker_environment import fake_docker_environment, host_container_backend

from vibesys.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_an_environment_without_candidate_sandboxes_is_refused_before_any_agent_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent container needs credentials for the CLI it would start.
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    loop_input = LoopInput.create(tmp_path / "input")
    agents = ScriptedAgents()
    request = loop_input.request(run_environment="docker").model_copy(
        update={"run_environment": fake_docker_environment()}
    )

    run = run_request(request, agents, backend_factory=host_container_backend)

    assert isinstance(run.error, ConfigurationError)
    message = str(run.error)
    assert "'docker' run environment" in message
    assert "isolated candidate sandboxes" in message
    assert agents.turns == []
    assert loop_input.sbatch_count() == 0
