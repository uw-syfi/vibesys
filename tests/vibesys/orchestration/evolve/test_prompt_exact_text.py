"""Byte-exact agent-facing text owned by the evolve plugin's prompt templates.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.evolve import PLUGIN
from vibesys.orchestration.evolve.models import EvolveOptions
from vs_runtime.api import RunFacts, RunStatus
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_FIXTURES = Path(__file__).parent / "fixtures" / "exact_prompts"
_FRONTIER = MetricSpace(
    objectives=(
        Objective(name="throughput", direction="max"),
        Objective(name="p99_latency", direction="min"),
    )
)


@pytest.mark.parametrize("agent", PLUGIN.agents, ids=lambda agent: agent.id)
def test_system_prompt_text_is_stable(agent: AgentRole) -> None:
    assert_exact_text(_FIXTURES / f"system-{agent.id}.txt", agent.system_prompt)


def _profiler_prompt(tmp_path: Path, profiler: str, metric_space: MetricSpace | None) -> str:
    prompts: list[str] = []

    def respond(
        role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        if role.id == "implementer":
            return {
                "summary": "reduced allocation overhead",
                "hypothesis": "reuse avoids repeated allocation",
                "expected_behavior": "lower latency",
            }
        if role.id == "judge":
            return {"analysis": "sound", "feedback": "", "verdict": "pass"}
        prompts.append(message)
        return {
            "analysis": "measured",
            "bottlenecks": "allocation",
            "suggestions": "reuse buffers",
            "perf_metric": 100.0,
            "perf_unit": "tokens/s",
        }

    async def scenario() -> None:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            responder=respond,
            supported_extra_tools={"profiler"},
            facts=RunFacts(
                domain_id="llm-serving" if profiler == "torch" else "generic",
                objective="Increase throughput.",
                profiler_id=profiler,
                benchmark_configured=True,
            ),
        )
        values: dict[str, object] = {
            "max_generations": 1,
            "children_per_generation": 1,
            "k_top_inspirations": 0,
            "k_random_inspirations": 0,
            "selection_temperature": 1.0,
            "frontier_bias": 0.7,
            "bootstrap_max_attempts": 1,
            "keep_deployments": False,
            "max_parallelism": 1,
        }
        if metric_space is not None:
            values["metric_space"] = metric_space
        try:
            status = await PLUGIN.orchestrate(run, EvolveOptions.model_validate(values))
            assert status is RunStatus.SUCCEEDED
        finally:
            await run.close()

    asyncio.run(scenario())
    return prompts[0].replace(str(tmp_path), "<ROOT>")


@pytest.mark.parametrize(
    ("profiler", "frontier"),
    [
        ("linux_cpu", False),
        ("linux_cpu", True),
        ("torch", False),
        ("torch", True),
    ],
)
def test_profiler_prompt_text_is_stable(tmp_path: Path, profiler: str, *, frontier: bool) -> None:
    prompt = _profiler_prompt(tmp_path, profiler, _FRONTIER if frontier else None)

    mode = "frontier" if frontier else "headline"
    assert_exact_text(_FIXTURES / f"profiler-{profiler}-{mode}.txt", prompt)
