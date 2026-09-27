"""Crash-independent resilience policy of the explicit multi plugin."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi import PLUGIN
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.multi.models import MultiState
from vibesys.schemas import HypothesisOutcome, Verdict
from vs_runtime.api import RunStatus, StructuredResponseError
from vs_runtime.api.testing import FakeRunHost, FakeWorkspace

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


DESIGNER, _PROFILER, IMPLEMENTER, JUDGE = PLUGIN.agents


def _options(**changes: object) -> BaseModel:
    return PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": 1,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 10,
            "memory_layout": "directories",
            **changes,
        }
    )


def _pre_round() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False,
        profile_focus="",
        reasoning="Existing evidence is sufficient.",
    )


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": "H-01",
            "hypothesis": "Batching removes per-request overhead.",
            "title": "Batch prefill",
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without an accuracy regression.",
            "reasoning": "The trace shows repeated launch overhead.",
        }
    )


def _implementation(**changes: object) -> ImplementerResponse:
    return ImplementerResponse.model_validate(
        {
            "summary": "Implemented batching.",
            "expected_behavior": "Fewer launches.",
            "hypothesis_outcome": "nominated",
            "evidence": "The local smoke check passed.",
            **changes,
        }
    )


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="The change matches the plan and evidence.",
        feedback="",
        verdict=Verdict.PASS,
    )


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, history, message))
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(path: Path, script: _Script, *, options: BaseModel | None = None) -> FakeRunHost:
    async def scenario() -> FakeRunHost:
        host = FakeRunHost(PLUGIN, project_root=path, responder=script.respond)
        try:
            assert await PLUGIN.orchestrate(host, options or _options()) is RunStatus.SUCCEEDED
            return host
        finally:
            await host.close()

    return asyncio.run(scenario())


def test_sparse_review_skips_judge_until_final_continuation_round(tmp_path: Path) -> None:
    script = _Script(
        _pre_round(),
        _plan(),
        _implementation(
            summary="Partial batching.",
            hypothesis_outcome=HypothesisOutcome.CONTINUE,
            next_step="Finish batching the decode path.",
        ),
        _implementation(summary="Finished batching."),
        _judge(),
    )

    host = _run(
        tmp_path,
        script,
        options=_options(max_rounds=2, judge_every=2),
    )

    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
        IMPLEMENTER.id,
        JUDGE.id,
    ]
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    assert state.search.rounds[0].judge_verdict == "deferred"
    assert state.search.rounds[1].judge_verdict == "pass"


def test_malformed_implementer_response_retries_before_judge(tmp_path: Path) -> None:
    script = _Script(
        _pre_round(),
        _plan(),
        StructuredResponseError(IMPLEMENTER.id, ImplementerResponse),
        _implementation(),
        _judge(),
    )

    host = _run(tmp_path, script)

    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
        IMPLEMENTER.id,
        JUDGE.id,
    ]
    implementer = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert [len(history) for _role, history, _message in implementer] == [0, 0]
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    assert state.search.rounds[0].attempts == 2


def test_explicit_plugin_pass_trajectory_matches_golden(tmp_path: Path) -> None:
    script = _Script(_pre_round(), _plan(), _implementation(), _judge())

    host = _run(tmp_path, script)
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    workspace = host.workspaces.root
    assert isinstance(workspace, FakeWorkspace)
    observed = {
        "roles": [role for role, _history, _message in script.calls],
        "member_ids": [session.member_id for session in host.agents.sessions],
        "rounds": [
            {
                "number": record.round_number,
                "hypothesis_id": record.hypothesis_id,
                "attempts": record.attempts,
                "judge_verdict": record.judge_verdict,
                "passed": record.passed,
            }
            for record in state.search.rounds
        ],
        "retained": workspace.retained,
        "artifacts": sorted(
            path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*") if path.is_file()
        ),
    }
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "pass_trajectory.json").read_text(encoding="utf-8")
    )

    assert observed == expected
