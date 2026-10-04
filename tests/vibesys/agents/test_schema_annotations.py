"""Planner contracts reach providers with their field guidance intact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agentshim.testing import FakeExecutor, scripted_turn

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.dynamic.models import ImplementPortfolioPlan, PortfolioPlan
from vibesys.orchestration.multi.contracts import PreRoundDecision
from vs_agent.api import AgentClient
from vs_agent.api.testing import fake_agentshim_driver

if TYPE_CHECKING:
    from pydantic import BaseModel


def _annotations(
    schema: object, path: tuple[str | int, ...] = ()
) -> dict[tuple[str | int, ...], object]:
    """Collect annotations by location, independently of dialect normalization."""
    result = {}
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"description", "title", "examples"} and not isinstance(value, dict):
                result[(*path, key)] = value
            else:
                result.update(_annotations(value, (*path, key)))
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            result.update(_annotations(value, (*path, index)))
    return result


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize(
    "response",
    [
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="Existing evidence suffices."
        ),
        OrchestratorPlan.model_validate(
            {
                "task": "Implement batching.",
                "pass_criteria": "Tests pass.",
                "reasoning": "Batching reduces overhead.",
            }
        ),
        PortfolioPlan(
            reasoning="Test batching.",
            workstreams=[
                {
                    "hypothesis_id": "H-01",
                    "title": "Batch",
                    "hypothesis": "Batching reduces overhead.",
                    "task": "Implement batching.",
                    "pass_criteria": "Tests pass.",
                }
            ],
        ),
        ImplementPortfolioPlan(
            reasoning="Test batching.",
            workstreams=[
                {
                    "hypothesis_id": "H-01",
                    "title": "Batch",
                    "hypothesis": "Batching reduces overhead.",
                    "task": "Implement batching.",
                    "pass_criteria": "Tests pass.",
                }
            ],
        ),
    ],
    ids=lambda response: type(response).__name__,
)
def test_planner_native_schema_preserves_field_annotations(
    tmp_path: Path, provider: str, response: BaseModel
) -> None:
    payload = response.model_dump(mode="json")
    executor = FakeExecutor(scripted_turn(provider, structured_output=payload))
    driver = fake_agentshim_driver(provider=provider, executor=executor)
    with AgentClient(driver, provider=provider) as client:
        result = client.invoke(
            kind="orchestrator",
            workspace=tmp_path,
            system_prompt="test",
            user_prompt="plan",
            response_cls=type(response),
            round_label="schema annotations",
        )
        argv = executor.requests[-1].argv
        if provider == "codex":
            schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        else:
            schema = json.loads(argv[argv.index("--json-schema") + 1])

    assert result == response
    expected = _annotations(type(response).model_json_schema())
    assert any(path[-1] == "description" and "properties" in path for path in expected)
    received = _annotations(schema)
    assert all(received.get(path) == value for path, value in expected.items())
