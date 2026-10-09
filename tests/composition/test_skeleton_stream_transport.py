"""The skeleton run, with its implementer behind a stream transport in a container.

The same full run as ``test_skeleton``, but the provider is the production driver over
agentshim's long-lived Claude and Codex transports, against scripted far ends. A fault plan
breaks the process or replaces the container in the middle of the implementer's turn, and the
run must still reach a typed terminal state without the provider seeing the turn twice.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import agentshim
import pytest
from tests.composition.test_skeleton import _assert_adopted, _started
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    CandidateResolver,
    CandidateWriter,
    Implementation,
    drive,
    finished,
    open_skeleton_world,
)
from tests.support.stream_peers import answering_with
from tests.support.stream_session_world import StreamHost, open_stream_host

from vs_agent.api import AgentExecutionPolicy, AgentSessionSpec
from vs_faults.api import Boundary, FaultPlan, FaultRule, ProcessFault
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.session_world import SessionHost

    from vs_core.api import TurnSpec
    from vs_runtime.api.core import AccessGuardedWorkspace

PROVIDERS = tuple(agentshim.stream_provider_names())


@dataclasses.dataclass
class _ContainerCandidateResolver(CandidateResolver):
    provider: str = "claude"

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The candidate worktree session, run by ``provider`` in a container."""
        spec = super().agent_spec(turn, workspace)
        assert spec is not None
        return dataclasses.replace(
            spec,
            provider=self.provider,
            policy=AgentExecutionPolicy(containerized=True, require_enforcement=False),
        )


def _plan(position: int, fault: ProcessFault) -> FaultPlan:
    rule = FaultRule(boundary=Boundary.PROCESS_OUTPUT, target="agent", at=position, fault=fault)
    return FaultPlan(seed=1, rules=(rule,))


def _open(root: Path, provider: str, plan: FaultPlan | None) -> StreamHost:
    writer = CandidateWriter(root)

    def answer() -> str:
        writer.commit_change()
        return json.dumps(writer.answer)

    resolver = _ContainerCandidateResolver(
        root,
        TemplateRenderer(root),
        roles=frozenset({IMPLEMENTER}),
        schemas={IMPLEMENTATION: Implementation},
        writer=writer,
        provider=provider,
    )
    return open_stream_host(provider, answering_with(provider, answer), resolver, plan)


async def _run(tmp_path: Path, provider: str, plan: FaultPlan | None) -> StreamHost:
    opened: list[StreamHost] = []

    def agents(root: Path) -> SessionHost:
        opened.append(_open(root, provider, plan))
        return opened[0].host

    with open_skeleton_world(tmp_path, SkeletonStrategy.unmeasured(), agents=agents) as world:
        process, now = await _started(world, None)
        await drive(process, start=now)
        assert finished(process)
        return opened[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_skeleton_run_adopts_through_a_stream_transport(
    tmp_path: Path, provider: str
) -> None:
    opened: list[StreamHost] = []

    def agents(root: Path) -> SessionHost:
        opened.append(_open(root, provider, None))
        return opened[0].host

    with open_skeleton_world(tmp_path, SkeletonStrategy.unmeasured(), agents=agents) as world:
        process, now = await _started(world, None)
        assert await drive(process, start=now) is None
        _assert_adopted(process, world)
    assert opened[0].executor.injected == []
    assert len(opened[0].peers.prompts()) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("fault", list(ProcessFault))
async def test_a_fault_in_the_implementer_turn_ends_the_run_typed_without_a_replay(
    tmp_path: Path, provider: str, fault: ProcessFault
) -> None:
    stream = await _run(tmp_path, provider, _plan(2, fault))
    prompts = stream.peers.prompts()
    assert len(prompts) == len(set(prompts))
    assert stream.executor.injected, "the scheduled fault never fired"
