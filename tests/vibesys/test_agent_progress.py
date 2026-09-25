"""Progress attribution for direct agent handles."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from io import StringIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from vibesys.orchestration.agents import _active_progress, _Agents, _LocalAgentHandle
from vibesys.run.integration import LocalRunIntegration
from vibesys.runtime import AgentDefinition
from vibesys.schemas import JudgeResponse, Verdict
from vs_agent.api import AgentBackend, AgentSpec, CandidateProgress, RoundProgress
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path


def _judge_fallback() -> JudgeResponse:
    return JudgeResponse(analysis="fallback", feedback="fallback-feedback", verdict=Verdict.FAIL)


def test_progress_rendering_is_loop_owned() -> None:
    assert RoundProgress(3, 24).label() == "Round 3/24"
    assert CandidateProgress(2, 8, 1, 4).label() == "Round 2/8 Cand 1/4"


def test_agent_progress_scope_restores_previous() -> None:
    agents = _Agents(cast("Any", object()))
    outer = RoundProgress(1, 3)
    inner = CandidateProgress(2, 3, 1, 2)

    with agents.progress(outer):
        with agents.progress(inner):
            pass
        assert _active_progress.get() is outer
    assert _active_progress.get() is None


def test_agent_turn_captures_current_progress(tmp_path: Path) -> None:
    integration = LocalRunIntegration()
    client = FakeAgentClient(driver_name="mock", provider="mock", model="mock-model")
    client.set_response("judge", _judge_fallback())
    resources = cast(
        "Any",
        SimpleNamespace(
            integration=integration,
            events=integration.events,
            workspace=tmp_path,
            run_log_file=StringIO(),
        ),
    )
    progress = RoundProgress(2, 5)
    agents = _Agents(cast("Any", object()))

    async def invoke() -> None:
        handle = _LocalAgentHandle(
            AgentDefinition("judge", AgentSpec(backend=AgentBackend.STUB)),
            resources,
            client,
            ExitStack(),
            ThreadPoolExecutor(max_workers=1),
            None,
            use_docker=True,
        )
        try:
            with agents.progress(progress):
                await handle.turn_structured(
                    "usr",
                    response_cls=JudgeResponse,
                    fallback_factory=_judge_fallback,
                    system_prompt="sys",
                    label="judge #1",
                )
        finally:
            await handle.close()

    try:
        asyncio.run(invoke())
        assert client.calls_for("judge")[0].progress is progress
    finally:
        integration.close()
