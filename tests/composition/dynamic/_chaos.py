"""Chaos runs of the dynamic search on the core path: generated agents and a seeded fault plan.

One seed decides everything a run varies: the fault plan (``vs_faults``), every agent
reply (generated from the schema each turn declares), the candidate each implementer
writes, and whether and when the operator stops the run. The run and everything under it
are production code over the scenario harness (:mod:`._harness`); the Fake cluster's
connector runs behind the plan's cluster wrapper, and the run's clock is simulated, so a
stuck run fails at the harness's budget instead of hanging. After the run, the shared
loop invariants (``tests.support.loop_invariants``), the core's liveness rules over the
committed record, and the chaos-only invariants below must hold.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tests.composition.dynamic._harness import (
    LoopInput,
    LoopRun,
    assert_run_live,
    load_envelope,
    run_request,
    simulated_clock,
)
from tests.support.loop_invariants import Invariant, RunRecords, Violation, check, terminal_event

from vibesys.api import RunStatus, RunStopped
from vibesys.dynamic_roles import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vs_agent.api import NULL_SKILL_SELECTION, AgentCapabilities, AgentOutputSchemaError
from vs_agent.api.testing import (
    AgentCrashError,
    FakeAgentClient,
    FaultyAgentClient,
    generated_replies,
)
from vs_core.testing.liveness import End
from vs_faults.api import (
    AgentFault,
    Boundary,
    ClusterFault,
    FaultPlan,
    handle_cluster_request_with,
    injected_faults,
)
from vs_project.api import Project
from vs_runtime.api import RunCleanupError, UnresolvedDispatchError
from vs_sim.api import OsThreads
from vs_sim.api.testing import VirtualTimeLimitError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.api import RunHandle
    from vs_agent.api import SessionStore, SkillSelection
    from vs_agent.api.testing import FakeInvocation
    from vs_sim.api import Lock

_THREADS = OsThreads()

ROLES = (ORCHESTRATOR.id, IMPLEMENTER.id, JUDGE.id)
#: Output faults: the agent's reply is wrong, so a schema or planning failure may follow.
_OUTPUT_FAULTS = frozenset(
    {
        AgentFault.MALFORMED,
        AgentFault.SCHEMA_INVALID,
        AgentFault.EXTRA_KEYS,
        AgentFault.WRONG_VALUES,
    }
)
#: Transport faults: the turn's fate is unknown, so its dispatch is unresolved.
_TRANSPORT_FAULTS = frozenset({AgentFault.CRASH, AgentFault.TIMEOUT})
# A faulted poll is retried after this interval (simulated seconds).
_POLL_S = 0.2
# Simulated seconds a faulted run may take: past it the run is stuck, not slow.
_BUDGET_S = 300.0


class ChaosInvariant:
    """Invariants only a chaos run checks (the shared ones live in ``loop_invariants``)."""

    HANG = "hang"
    STATE_UNLOADABLE = "state_unloadable"
    UNLIVE = "unlive"
    UNEXPLAINED_END = "unexplained_end"


@dataclass(frozen=True)
class Injected:
    """The faults a run actually suffered, which decide the endings it may have."""

    agent: frozenset[AgentFault] = frozenset()
    #: Cluster transport faults the connector wrapper injected. Which Slurm call
    #: receives one depends on the run's pace, so only the kinds are recorded.
    cluster: frozenset[ClusterFault] = frozenset()
    #: The user's stop reached the run during it (not merely scheduled).
    stop_delivered: bool = False


#: Each way a run may raise instead of reporting, with the faults that can cause it. The
#: map is closed: an error type outside it is a violation whatever was injected. A transport
#: fault leaves a turn's fate unknown; an output fault makes a reply wrong, which schema
#: errors report. A cluster fault can leave a job's cancellation unknown, so cleanup cannot
#: close the evaluation services and ends the run with a typed ``RunCleanupError``
#: (decision D294).
_EXPLAINED_BY: dict[type[BaseException], frozenset[AgentFault | ClusterFault]] = {
    UnresolvedDispatchError: _TRANSPORT_FAULTS,
    AgentCrashError: frozenset({AgentFault.CRASH}),
    AgentOutputSchemaError: _OUTPUT_FAULTS,
    RunCleanupError: frozenset(ClusterFault),
}
#: Faults that can make a run report failure rather than success: a lost or wrong turn is
#: retried a bounded number of times, and a cluster fault can lose a measurement.
_FAILS_A_RUN = _TRANSPORT_FAULTS | _OUTPUT_FAULTS | frozenset(ClusterFault)


def unexplained_end(
    error: BaseException | None, status: RunStatus | None, injected: Injected
) -> str | None:
    """Return why a run's ending is none ``injected`` can explain (``None``: it is explained).

    ``status`` is how the run reported when it did not raise (``error`` is then ``None``).
    """
    if error is None:
        return _unexplained_report(status, injected)
    if isinstance(error, RunStopped):
        return _unexplained_stop(injected)
    for error_type, causes in _EXPLAINED_BY.items():
        if isinstance(error, error_type):
            if (injected.agent | injected.cluster) & causes:
                return None
            return f"{error_type.__name__} without a fault that can cause it"
    return "an ending no fault is declared to cause"


def _unexplained_stop(injected: Injected) -> str | None:
    return None if injected.stop_delivered else "stopped without a delivered stop"


def _unexplained_report(status: RunStatus | None, injected: Injected) -> str | None:
    if status is RunStatus.STOPPED:
        return _unexplained_stop(injected)
    if status is RunStatus.FAILED and not (injected.agent | injected.cluster) & _FAILS_A_RUN:
        return "reported failure without a fault that can cause it"
    return None


def repro(seed: int) -> str:
    """Return the one-line command that reruns ``seed``."""
    return (
        "uv run pytest tests/composition/dynamic/test_chaos.py "
        f"-k 'seed_{seed}]' -p no:randomly  # or CHAOS_SEEDS={seed}"
    )


@dataclass
class ChaosAgents:
    """Generated agents for every role behind the plan's agent-turn faults."""

    plan: FaultPlan
    #: Request a stop when the ``stop_at``-th agent turn starts (``None``: never).
    stop_at: int | None = None
    stop_delivered: bool = False
    handle: RunHandle | None = None
    faulty: FaultyAgentClient | None = None
    _counts: Counter[str] = field(default_factory=Counter)
    _lock: Lock = field(default_factory=_THREADS.lock)

    def client(
        self,
        *,
        session_store: SessionStore | None = None,
        skill_selection: SkillSelection = NULL_SKILL_SELECTION,
        **_kwargs: object,
    ) -> FaultyAgentClient:
        """Build the run's client: a production-capable Fake behind the fault wrapper."""
        fake = FakeAgentClient(
            capabilities=AgentCapabilities(session_reuse=True, provider_session_resume=True),
            session_reuse=True,
            session_store=session_store,
            skill_selection=skill_selection,
        )
        replies = generated_replies(self.plan)

        def answer(invocation: FakeInvocation) -> BaseModel:
            self._act(invocation)
            return replies(invocation)

        for role in ROLES:
            fake.set_response(role, answer)
        self.faulty = FaultyAgentClient(fake, self.plan)
        return self.faulty

    def _act(self, invocation: FakeInvocation) -> None:
        """Stop the run on the planned turn, and write the candidate (implementers)."""
        with self._lock:
            self._counts[invocation.kind] += 1
            ordinal = self._counts[invocation.kind]
            turns = self._counts.total()
        if turns == self.stop_at and self.handle is not None:
            # The user's Ctrl-C reaches a run as this request (the engine's signal
            # handler calls it); the turn itself continues.
            self.handle.stop()
            self.stop_delivered = True
        candidate = invocation.workspace / "queue.py"
        if invocation.kind == IMPLEMENTER.id and candidate.is_file():
            rng = self.plan.rng("act", invocation.kind, ordinal)
            _edit(candidate, rng.randint(-1, 9), rng.choice((None, 3, 12)))


def _edit(candidate: Path, value: int, required: int | None) -> None:
    text = f"VALUE = {value}\n" + (f"REQUIRED = {required}\n" if required is not None else "")
    candidate.write_text(text, encoding="utf-8")


@dataclass
class ChaosRun:
    """One finished chaos run and every invariant violation it showed."""

    seed: int
    plan: FaultPlan
    run: LoopRun | None
    violations: list[Violation | tuple[str, str]]
    injected: list[str]
    stop_at: int | None = None

    def outcome(self) -> str:
        """Return how the run ended, in one line."""
        if self.run is None:
            return "no end"
        return f"succeeded={self.run.succeeded} status={self.run.status} error={self.run.error!r}"[
            :300
        ]

    def summary(self) -> dict[str, object]:
        """Return a JSON-ready summary: outcome, faults, violations."""
        return {
            "seed": self.seed,
            "outcome": self.outcome(),
            "injected": self.injected,
            "stop_at": self.stop_at,
            "violations": [str(item) for item in self.violations],
        }

    def report(self) -> str:
        """Return the failure report: seed, repro, injected faults, violations."""
        lines = [
            f"seed {self.seed}: {len(self.violations)} violation(s); {self.outcome()}",
            repro(self.seed),
        ]
        lines += [f"  injected: {item}" for item in self.injected]
        if self.stop_at is not None:
            lines.append(f"  stop requested at agent turn {self.stop_at}")
        lines += [f"  VIOLATION {item}" for item in self.violations]
        return "\n".join(lines)


def plan_for(seed: int, *, faults: int | None = None) -> FaultPlan:
    """Return the fault plan seed ``seed`` runs under (0 to 4 faults)."""
    count = faults if faults is not None else seed % 5
    return FaultPlan.generate(
        seed,
        targets={Boundary.AGENT_TURN: ROLES, Boundary.TOOL_CALL: (), Boundary.CLUSTER: ()},
        faults=count,
    )


def run_chaos(base: Path, seed: int, plan: FaultPlan | None = None) -> ChaosRun:
    """Run the dynamic search once under ``seed`` and check every invariant."""
    plan = plan if plan is not None else plan_for(seed)
    faults_dir = base / "faults"
    faults_dir.mkdir(parents=True)
    loop_input = LoopInput.create(base, poll_interval_s=_POLL_S)
    # The in-process Fake cluster answers behind the plan's cluster faults: a faulted call
    # fails as a failing cluster does and never reaches it.
    loop_input.connector.intercept(
        lambda sent, answer: handle_cluster_request_with(plan, faults_dir / "cluster", answer, sent)
    )
    rng = plan.rng("options")
    agents = ChaosAgents(plan, stop_at=rng.choice((None, None, None, rng.randint(1, 8))))
    request = loop_input.request(
        max_rounds=rng.randint(1, 3),
        max_in_flight=rng.randint(1, 3),
        max_retries_per_round=rng.randint(1, 2),
    )

    def remember(handle: RunHandle) -> None:
        agents.handle = handle

    run = run_request(
        request,
        agents,
        verify=False,
        budget_s=_BUDGET_S,
        on_handle=remember,
        clock=simulated_clock(),
        slurm_process=loop_input.connector,
        state_stores=loop_input.state_stores,
    )
    injected = [str(item) for item in (agents.faulty.injected if agents.faulty else [])]
    injected += [json.dumps(item) for item in injected_faults(faults_dir / "cluster")]
    suffered = Injected(
        agent=frozenset(item[2] for item in (agents.faulty.injected if agents.faulty else [])),
        cluster=frozenset(
            ClusterFault(str(item["fault"])) for item in injected_faults(faults_dir / "cluster")
        ),
        stop_delivered=agents.stop_delivered,
    )
    violations: list[Violation | tuple[str, str]] = []
    if isinstance(run.error, VirtualTimeLimitError):
        violations.append((ChaosInvariant.HANG, f"run did not end within {_BUDGET_S} s"))
    elif (reason := unexplained_end(run.error, run.status, suffered)) is not None:
        detail = repr(run.error) if run.error is not None else run.status
        violations.append((ChaosInvariant.UNEXPLAINED_END, f"{reason}: {detail}"))
    violations += _records_violations(loop_input, run, request.project_root)
    chaos = ChaosRun(seed, plan, run, violations, injected, agents.stop_at)
    log = os.environ.get("CHAOS_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(chaos.summary()) + "\n")
    return chaos


def _records_violations(
    loop_input: LoopInput, run: LoopRun, root: Path
) -> list[Violation | tuple[str, str]]:
    """Check the shared invariants and the core's liveness over what the run left behind."""
    found: list[Violation | tuple[str, str]] = []
    try:
        envelope = load_envelope(
            Project.open(root, state_stores=loop_input.state_stores), run.run_id
        )
    # lint-waiver: LW-150099 [BLE001]; any failure to load the state the run left is the
    # > finding; naming types would hide a new failure mode.
    except Exception as error:  # noqa: BLE001
        return [(ChaosInvariant.STATE_UNLOADABLE, repr(error))]
    jobs_dir = loop_input.cluster / "jobs"
    jobs = (
        {item.name: item.read_text(encoding="utf-8").strip() for item in jobs_dir.iterdir()}
        if jobs_dir.is_dir()
        else {}
    )
    records = RunRecords(
        events=[event.model_dump(mode="json") for event in run.events],
        cluster_jobs=jobs,
        envelope=envelope,
    )
    found += [
        violation
        for violation in check(records, roots=(root,))
        # The Fake agent client records no token usage.
        if violation.invariant is not Invariant.USAGE_UNRECORDED
    ]
    ended = terminal_event(records)
    if envelope is not None and run.error is None:
        try:
            stopped = ended is not None and ended.get("status") == "interrupted"
            assert_run_live(envelope, End.STOPPED if stopped else End.TERMINAL)
        except AssertionError as error:
            found.append((ChaosInvariant.UNLIVE, str(error)))
    return found


__all__ = ["ChaosInvariant", "Injected", "plan_for", "run_chaos", "unexplained_end"]
