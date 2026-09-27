"""Exact public-output goldens for complete issue-queue trajectories."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING, TypedDict, cast

import pytest
from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_prompt_snapshot,
    prompt_text,
)

from vibesys.orchestration.issue_queue import PLUGIN, IssueQueueOptions, IssueQueueState
from vs_runtime.api import RunFacts, RunStatus
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_ARTIFACT_PATHS = {
    ".vibesys/issue-tool-policy.json",
    ".vibesys/issue-tracker.json",
    ".vibesys/issues/0001-build-inference-server-for-the-reference.md",
    ".vibesys/issues/INDEX.md",
    "issues.json",
    "progress.md",
}


def _options() -> IssueQueueOptions:
    return IssueQueueOptions.model_validate(
        {
            "max_rounds": 1,
            "max_attempts_per_issue": 3,
            "max_issues_per_perf_eval": 3,
            "load_levels": (
                {"rate": 1, "duration": 20, "max_tokens": 128},
                {"rate": 4, "duration": 20, "max_tokens": 128},
                {"rate": 8, "duration": 20, "max_tokens": 128},
            ),
        }
    )


def _implementation() -> dict[str, object]:
    return {
        "issue_id": 1,
        "summary": "Built the inference server.",
        "files_touched": ("server.py",),
        "self_check": "Ran the accuracy checker locally.",
    }


def _review() -> dict[str, object]:
    return {
        "issue_id": 1,
        "analysis": "Reviewed the diff and the accuracy checks.",
        "feedback": "",
        "verdict": "pass",
        "new_issues_filed": (),
    }


def _performance(*, detailed: bool) -> dict[str, object]:
    load_levels: tuple[dict[str, object], ...] = ()
    analysis = "First benchmark run, with no prior iteration to compare."
    feedback: tuple[str, ...] = ()
    latency_trend = "improved"
    if detailed:
        load_levels = (
            {
                "target_rate": 8.0,
                "actual_rate": 7.9,
                "num_requests": 100,
                "num_completed": 100,
                "num_failed": 0,
                "duration": 20.0,
                "throughput": {
                    "request_throughput": 7.9,
                    "token_throughput": 1011.2,
                },
                "ttft": {
                    "mean_ms": 42.0,
                    "p50_ms": 40.0,
                    "p90_ms": 55.0,
                    "p95_ms": 60.0,
                    "p99_ms": 70.0,
                },
                "tpot": {
                    "mean_ms": 9.0,
                    "p50_ms": 8.5,
                    "p90_ms": 11.0,
                    "p95_ms": 12.0,
                    "p99_ms": 15.0,
                },
                "total_latency": {
                    "mean_ms": 900.0,
                    "p50_ms": 880.0,
                    "p90_ms": 1000.0,
                    "p95_ms": 1050.0,
                    "p99_ms": 1200.0,
                },
            },
        )
        analysis = "Throughput saturates around rate=8; TTFT stays flat below that."
        feedback = ("Rate 8 is the saturation point; try rate 16 next iteration.",)
        latency_trend = "mixed"
    return {
        "analysis": analysis,
        "metrics": {"load_levels": load_levels, "extra": {}},
        "evaluator_feedback": feedback,
        "new_issue_ids": (),
        "throughput_trend": "improved",
        "latency_trend": latency_trend,
    }


class _RecordedCall(TypedDict):
    role: str
    system_prompt: str
    history: list[str]
    message: str
    response_schema: str | None


class _Script:
    def __init__(self, *replies: object) -> None:
        self._replies = deque(replies)
        self.calls: list[_RecordedCall] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        self.calls.append(
            {
                "role": role.id,
                "system_prompt": role.system_prompt,
                "history": list(history),
                "message": message,
                "response_schema": response.__name__ if response is not None else None,
            }
        )
        return self._replies.popleft()


def _artifact_snapshot(root: Path) -> dict[str, str]:
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert actual == _ARTIFACT_PATHS
    return {
        relative: (root / relative).read_text(encoding="utf-8")
        for relative in sorted(_ARTIFACT_PATHS)
    }


def _canonical_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.rstrip().splitlines()) + "\n"


def _assert_golden(
    scenario: str,
    observed: dict[str, object],
    *,
    workspace: Path,
) -> None:
    calls = cast("list[_RecordedCall]", observed["calls"])
    for index, call in enumerate(calls, start=1):
        role = call["role"]
        system_prompt = call["system_prompt"]
        message = call["message"]
        assert_prompt_snapshot(
            "issue_queue",
            f"{index:02d}-{role}",
            scenario,
            _canonical_text(prompt_text(system_prompt, message)),
            workspace=workspace,
        )

    artifacts = observed["artifacts"]
    assert isinstance(artifacts, dict)
    for relative, content in artifacts.items():
        assert isinstance(relative, str)
        assert isinstance(content, str)
        assert_board_snapshot(
            "issue_queue",
            scenario,
            relative,
            _canonical_text(content),
            workspace=workspace,
        )

    for name in ("trajectory", "projection", "retained"):
        assert_board_snapshot(
            "issue_queue",
            scenario,
            f"_{name}.json",
            json.dumps(observed[name], indent=2, sort_keys=True) + "\n",
            workspace=workspace,
        )


@pytest.mark.parametrize("scenario", ["pass", "perf_eval"])
def test_public_policy_trajectory_matches_golden(tmp_path: Path, scenario: str) -> None:
    async def run() -> dict[str, object]:
        script = _Script(
            _implementation(),
            _review(),
            _performance(detailed=scenario == "perf_eval"),
        )
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="llm-serving",
                objective="Maximize token throughput without losing correctness.",
                reference_location="reference/model.py",
                accuracy_command="uv run check-accuracy",
                benchmark_command="uv run benchmark",
                profiler_id="torch",
                environment_notes="Use CUDA with bfloat16.",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            responder=script.respond,
        )
        try:
            status = await PLUGIN.orchestrate(run, _options())
            assert status is RunStatus.SUCCEEDED
            state = await run.state.load(IssueQueueState)
            assert state is not None
            assert PLUGIN.project is not None
            return {
                "calls": script.calls,
                "artifacts": _artifact_snapshot(tmp_path),
                "trajectory": {
                    "status": status.value,
                    "logs": [call.message for call in run.observations.calls],
                    "calls": [
                        {
                            "role": call["role"],
                            "prior_messages": len(call["history"]),
                            "response_schema": call["response_schema"],
                        }
                        for call in script.calls
                    ],
                    "commits": [
                        {
                            "label": commit.label,
                            "state": commit.value.model_dump(mode="json"),
                        }
                        for commit in run.state.commits
                    ],
                },
                "projection": PLUGIN.project(state).model_dump(mode="json"),
                "retained": run.workspaces.root.retained,
            }
        finally:
            await run.close()

    _assert_golden(scenario, asyncio.run(run()), workspace=tmp_path)
