"""A provider quota error during a core run is handled by the run's policy.

The production loop drives the skeleton search over the real shell, executors and Fake
cluster, with the agent client behind ``install_capacity_gate`` as the host installs it. The
Fake provider raises the quota error for its first calls. The policy decides whether the
turn pauses for the operator, waits for the reset and is sent again, or fails the turn, and
the strategy sees the outcome: a finished attempt or a failed one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    LEASE,
    CandidateResolver,
    CandidateWriter,
    Implementation,
    open_skeleton_world,
)

from vs_agent.api import (
    AgentClient,
    AgentQuotaError,
    Attribution,
    NullAgentEventSink,
    QuotaCondition,
    QuotaPlan,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider, FakeTurnScript
from vs_core.api import RunStatus
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import CoreRunHost, RunControlBridge, RunLoopConfig, drive_core
from vs_runtime.api.infrastructure import (
    CapacityHandling,
    QuotaAction,
    QuotaPolicy,
    RunControlTransition,
    RunControlTransitionKind,
    RuntimeRunControlChannel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeCapacityTimer, ProbedRunClock
from vs_sim.api import OsThreads
from vs_sim.api.testing import ManualClock, clock_from

if TYPE_CHECKING:
    from pathlib import Path

START = 1_000_000.0
"""The capacity timer's epoch second at the start of the run."""


class _Sink(NullAgentEventSink):
    def __init__(self) -> None:
        self.events: list[str] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del error, plan, where
        self.events.append("paused")

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, waited_seconds, where
        self.events.append(f"resumed:{reason}")

    def quota_abandoned(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, reason, waited_seconds, where
        self.events.append("abandoned")


class _NoSteers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


@dataclass
class _Played:
    sink: _Sink = field(default_factory=_Sink)
    transitions: list[RunControlTransitionKind] = field(default_factory=list)
    timer: FakeCapacityTimer | None = None
    writer: CandidateWriter | None = None
    outcome: str | None = None
    status: RunStatus | None = None


async def _play(
    tmp_path: Path, policy: QuotaPolicy, error: AgentQuotaError, *, stops: int
) -> _Played:
    played = _Played()

    def on_transition(transition: RunControlTransition) -> None:
        played.transitions.append(transition.kind)
        # The operator resumes a run the moment it reports PAUSED, unless the policy's own
        # wait is what ends the pause.
        if transition.kind is RunControlTransitionKind.PAUSED and played.timer is None:
            channel.resume()

    channel = RuntimeRunControlChannel(on_transition, threads=OsThreads())
    if policy.action is QuotaAction.WAIT:
        played.timer = FakeCapacityTimer(channel, start=START)

    def agents(root: Path) -> SessionHost:
        writer = CandidateWriter(root)
        played.writer = writer
        script = FakeTurnScript(answers=(*([error] * stops), writer.answer))
        client = AgentClient(
            FakeProvider(script=script, on_turn=writer, threads=OsThreads()),
            clock=ManualClock(),
            threads=OsThreads(),
        )
        install_capacity_gate(
            client,
            control=channel,
            events=played.sink,
            handling=CapacityHandling.for_policy(policy, OsThreads(), played.timer),
            clock=ManualClock(),
        )
        resolver = CandidateResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
        )
        return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())

    clock = ProbedRunClock(clock_from(1.0))
    with open_skeleton_world(tmp_path, SkeletonStrategy(), agents=agents) as world:
        process = world.runtime()
        controls = RunControlBridge(
            channel,
            _NoSteers(),  # type: ignore[arg-type]  # only stores steer text; none is sent
        )
        host = CoreRunHost(process.shell, process.delivery, clock, controls)
        process.shell.start("skeleton", now_at=1.0, lease_duration=LEASE)
        await drive_core(
            host,
            RunLoopConfig(
                host_id="loop",
                lease_duration=LEASE,
                control_poll_interval=1.0,
                min_sleep=0.05,
                max_dispatches=500,
            ),
        )
        record = process.shell.record.envelope
        played.status = record.core.run.status
        assert record.core.run.result is not None
        played.outcome = record.core.run.result.outcome
    return played


def _quota(resets_at: float | None = None) -> AgentQuotaError:
    return AgentQuotaError(
        "claude", QuotaCondition.QUOTA_EXHAUSTED, "You've hit your limit", resets_at
    )


@pytest.mark.parametrize("stops", [1, 3])
async def test_a_paused_run_resumes_and_the_turn_is_sent_again(tmp_path: Path, stops: int) -> None:
    played = await _play(tmp_path, QuotaPolicy(), _quota(), stops=stops)
    assert played.status == RunStatus.TERMINAL
    assert played.outcome == "success"
    assert played.sink.events == ["paused", "resumed:operator"] * stops
    assert played.transitions.count(RunControlTransitionKind.PAUSED) == stops
    assert played.writer is not None
    assert played.writer.turns == stops + 1, "the same turn is sent again after each resume"


async def test_a_wait_policy_resumes_by_itself_when_the_reported_reset_arrives(
    tmp_path: Path,
) -> None:
    policy = QuotaPolicy(QuotaAction.WAIT, wait_seconds=3600.0)
    played = await _play(tmp_path, policy, _quota(START + 600), stops=1)
    assert played.outcome == "success"
    assert played.timer is not None
    assert played.timer.waits == [605.0], "the reset plus the policy's margin"
    assert played.sink.events == ["paused", "resumed:wait_elapsed"]


@pytest.mark.parametrize(
    "policy",
    [
        QuotaPolicy(QuotaAction.FAIL),
        QuotaPolicy(QuotaAction.WAIT, wait_seconds=60.0),
    ],
    ids=["fail", "wait budget spent"],
)
async def test_a_policy_that_gives_up_fails_the_turn_and_the_search_sees_it(
    tmp_path: Path, policy: QuotaPolicy
) -> None:
    played = await _play(tmp_path, policy, _quota(START + 600), stops=1)
    assert played.status == RunStatus.TERMINAL
    assert played.outcome == "failure"
    assert played.sink.events == ["abandoned"]
    assert played.transitions == [], "a run that gives up never pauses"
