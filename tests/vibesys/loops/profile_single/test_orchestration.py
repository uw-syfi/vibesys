"""Profile single descriptor, projection, and visible round order."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from vibesys.agent_run.attempts import AttemptDecision
from vibesys.agent_run.options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.loops.profile_single.orchestration import (
    InvalidStrategyOptionsError,
    ProfileGuidedSingleAgentOrchestrator,
    ProfileSingleProjector,
    load_options,
)
from vibesys.loops.profile_single.session import ProfileSingleSession
from vibesys.orchestration.view import RunStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor


def _options(**updates: object) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 2,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 1,
        "memory_layout": "files",
        "profile_guided": ProfileGuidedInput(command=("profile",)),
    }
    values.update(updates)
    return AgentOrchestrationOptions.model_validate(values)


def _descriptor(**updates: object) -> OrchestrationDescriptor:
    return descriptor_from_options(
        _options(**updates), orchestration_id="profile-guided-single-agent"
    )


def test_options_reject_wrong_strategy_and_incompatible_settings() -> None:
    descriptor = _descriptor()
    assert load_options(descriptor).profile_guided is not None
    orchestrator = ProfileGuidedSingleAgentOrchestrator(descriptor)
    assert orchestrator.setup.state_namespace == "profile_single"
    assert orchestrator.setup.start_hints is not None
    assert orchestrator.setup.start_hints.expected_roles == (
        "orchestrator",
        "implementer",
    )

    with pytest.raises(ValueError, match="Unsupported agent orchestration"):
        load_options(descriptor.model_copy(update={"id": "multi-agent"}))
    with pytest.raises(InvalidStrategyOptionsError, match="profile_guided"):
        load_options(_descriptor(profile_guided=None))
    with pytest.raises(InvalidStrategyOptionsError, match="interface"):
        load_options(_descriptor(interface="native"))
    with pytest.raises(InvalidStrategyOptionsError, match="memory_layout"):
        load_options(_descriptor(memory_layout="unknown"))


def test_projector_uses_profile_single_namespace_and_ignores_other_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = AgentRunState()
    load = MagicMock(return_value=state)
    projected = MagicMock(return_value="view")
    monkeypatch.setattr("vibesys.loops.profile_single.orchestration.load_agent_run_state", load)
    monkeypatch.setattr("vibesys.loops.profile_single.orchestration.project_run_view", projected)
    projector = ProfileSingleProjector()
    project = MagicMock()

    assert projector.view(project, "run-1", status=RunStatus.ACTIVE, loop="single") == "view"
    load.assert_called_once_with(project, "run-1", namespace="profile_single")
    assert projector.project_committed("multi", state, run_id="run-1") is None
    assert projector.project_committed("profile_single", state, run_id="run-1") == "view"
    assert projected.call_args.kwargs["loop"] == "profile-guided-single-agent"


class _Session:
    def __init__(self, *, fail_turn: bool = False) -> None:
        self.calls: list[str] = []
        self.has_next_round = True
        self.fail_turn = fail_turn

    @asynccontextmanager
    async def round_scope(self) -> AsyncIterator[None]:
        self.calls.append("scope")
        yield

    async def select_hypothesis(self) -> str:
        self.calls.append("select")
        return "selected"

    def remaining_attempts(self, _selected: str) -> range:
        return range(1, 3)

    async def begin_attempt(self, _selected: str, retry: int) -> None:
        self.calls.append(f"begin:{retry}")

    async def combined_turn(self, _selected: str) -> AttemptDecision:
        self.calls.append("combined")
        if self.fail_turn:
            raise RuntimeError("combined turn failed")  # noqa: TRY003  # test-only failure seam
        return (
            AttemptDecision.RETRY if self.calls.count("combined") == 1 else AttemptDecision.OFFICIAL
        )

    async def official_gates(self, _selected: str) -> bool:
        self.calls.append("gates")
        return True

    async def commit_round(self, _selected: str) -> None:
        self.calls.append("commit")
        self.has_next_round = False

    async def finish(self) -> bool:
        self.calls.append("finish")
        return True

    async def close(self) -> None:
        self.calls.append("close")


def test_run_orders_attempts_gates_commit_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session()
    monkeypatch.setattr(ProfileSingleSession, "open", AsyncMock(return_value=session))
    boundary = AsyncMock()
    ctx = cast("RunContext", SimpleNamespace(control=SimpleNamespace(boundary=boundary)))

    assert asyncio.run(ProfileGuidedSingleAgentOrchestrator(_descriptor()).run(ctx))
    assert session.calls == [
        "scope",
        "select",
        "begin:1",
        "combined",
        "begin:2",
        "combined",
        "gates",
        "commit",
        "finish",
        "close",
    ]
    boundary.assert_awaited_once()


def test_run_closes_session_when_combined_turn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session(fail_turn=True)
    monkeypatch.setattr(ProfileSingleSession, "open", AsyncMock(return_value=session))
    ctx = cast("RunContext", SimpleNamespace(control=SimpleNamespace(boundary=AsyncMock())))
    with pytest.raises(RuntimeError, match="combined turn failed"):
        asyncio.run(ProfileGuidedSingleAgentOrchestrator(_descriptor()).run(ctx))
    assert session.calls[-1] == "close"
    assert "commit" not in session.calls
