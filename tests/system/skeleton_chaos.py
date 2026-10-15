"""Chaos on the skeleton search: a seeded fault plan, a stop at a seeded point, and the oracles.

One seed decides everything a run varies: the fault plan (``vs_faults``: agent-turn faults on the
skeleton's implementer role, cluster faults on its measurements), the skeleton's shape (how many
turns and peers, so how much there is to break), and whether and when the operator stops the run.
The run is the production shell, loop and executors over the skeleton world on a virtual clock, so
a stuck run fails at the loop's dispatch cap or the virtual-time budget instead of hanging.

Host crashes are not drawn here: the crash sweeps own them. What this module checks is that the
shell, handed typed failures from an agent and a cluster and a stop, ends the run in a way the
injected faults explain (``unexplained_end``), leaves no job behind, and leaves a core record that passes the liveness rules.

Faults enter at the interfaces the shell consumes. An agent fault is a ``FaultyAgentClient`` over
the world's client (the turn runs, then fails as a real CLI fails). A cluster fault is a
``FaultyCluster`` over the Fake cluster that answers the Cluster operation the faulted Slurm call
belongs to with the typed outcome the real ``SlurmCluster`` gives that failure (``ClusterUnknown``);
the connector-level parsing of those failures is the vs-slurm contract suites' to prove.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.loop_invariants import RunRecords, cluster_jobs
from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import (
    IMPLEMENTATION,
    IMPLEMENTER,
    Implementation,
    SkeletonConfig,
    SkeletonStrategy,
)
from tests.support.skeleton_world import (
    LEASE,
    CandidateResolver,
    CandidateWriter,
    open_skeleton_world,
)

from vibesys.api import RunStatus, RunStopped
from vs_agent.api import AgentClient, AgentOutputSchemaError
from vs_agent.api.testing import (
    AgentCrashError,
    FakeAgentInvocationStore,
    FakeAgentSessions,
    FakeProvider,
    FaultyAgentClient,
)
from vs_core.api import RunStatus as CoreStatus
from vs_core.api import StopMode, waits
from vs_core.api.testing import Budget, End, Journal, liveness, sample_limits
from vs_faults.api import AgentFault, Boundary, ClusterFault, ClusterOperation, FaultPlan
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import RunCleanupError, RuntimeContractError
from vs_runtime.api.core import (
    CoreRunHost,
    RunControlBridge,
    RunLoopConfig,
    drive_core,
)
from vs_runtime.api.infrastructure import RuntimeRunControlChannel
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
from vs_runtime.api.testing import ProbedRunClock
from vs_sim.api.testing import ManualClock, clock_from, loop_threads
from vs_slurm.api import ClusterUnknown, Ended, Queued, SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState
    from vs_runtime.api.core import RunOutcome
    from vs_sim.api.testing import VirtualClock
    from vs_slurm.api import (
        ClusterCancelOutcome,
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        ClusterObservation,
        ClusterSubmitOutcome,
        ClusterTarget,
        SlurmBatchRequest,
        SlurmJobRequest,
    )

#: The agent role the skeleton runs (the fault plan's target at the agent-turn boundary).
ROLES = (IMPLEMENTER.root,)
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
#: Simulated seconds a faulted run may take: past it the run is stuck, not slow.
BUDGET_S = 1500.0
_MAX_DISPATCHES = 4000
_MAX_STOP_TICK = 14


class ChaosInvariant:
    """Invariants only a chaos run checks (the shared ones live in ``loop_invariants``)."""

    HANG = "hang"
    UNLIVE = "unlive"
    UNEXPLAINED_END = "unexplained_end"


@dataclass(frozen=True)
class Injected:
    """The faults a run actually suffered, which decide the endings it may have."""

    agent: frozenset[AgentFault] = frozenset()
    #: Cluster faults the cluster wrapper injected. Which call receives one depends on the
    #: run's pace, so only the kinds are recorded.
    cluster: frozenset[ClusterFault] = frozenset()
    #: The user's stop reached the run during it (not merely scheduled).
    stop_delivered: bool = False


class UnresolvedDispatchError(RuntimeContractError):
    """Stands in for a typed transport failure whose provider dispatch is unresolved."""


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
    error: BaseException | None,
    status: RunStatus | None,
    injected: Injected,
) -> str | None:
    """Return why a run's ending is none ``injected`` can explain (``None``: it is explained).

    ``status`` is how the run reported when it did not raise (``error`` is then ``None``).
    No cluster fault explains a run that stopped itself at its deadline: past the deadline core
    cancels every job it still owns, so even a job whose fate is hidden cannot hold the run.
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


def ending_of(
    outcome: RunOutcome | None, error: BaseException | None
) -> tuple[BaseException | None, RunStatus | None]:
    """Return a skeleton run's ending as the oracle reads it: (error, reported status).

    A terminal record reports ``COMPLETED`` for a success, ``STOPPED`` for a cancellation and
    ``FAILED`` otherwise. A refused executor or an unfinished record is a contract error,
    whatever was injected.
    """
    if error is not None:
        return error, None
    assert outcome is not None
    if outcome.refusal is not None:
        return RuntimeContractError(f"executor refused: {outcome.refusal.detail}"), None
    if outcome.status is not CoreStatus.TERMINAL:
        return RuntimeContractError(f"run left with status {outcome.status.value}"), None
    result = outcome.result.outcome if outcome.result is not None else None
    if result == "success":
        return None, RunStatus.COMPLETED
    if result == "cancelled":
        return None, RunStatus.STOPPED
    return None, RunStatus.FAILED


def repro(seed: int) -> str:
    """Return the one-line command that reruns ``seed``."""
    return (
        "uv run pytest tests/system/chaos/test_chaos.py "
        f"-k 'seed_{seed}]' -p no:randomly  # or CHAOS_SEED_RANGE={seed}"
    )


@dataclass
class Ticks:
    """The run's progress as the stop schedule counts it, and the stop itself.

    A tick is one Cluster call or one wait of the run loop, in the order they happen (both run
    on the loop's own timeline, so the order does not depend on thread scheduling). The stop is
    requested on tick ``stop_at`` (``None``: never).
    """

    stop_at: int | None
    channel: RuntimeRunControlChannel
    count: int = 0
    stop_delivered: bool = False

    def tick(self) -> None:
        """Count one tick; request the stop when it is the scheduled one."""
        self.count += 1
        if self.count == self.stop_at and not self.stop_delivered:
            # The operator's Ctrl-C reaches a run as this request.
            self.channel.request_stop(StopMode.DRAIN)
            self.stop_delivered = True


def _operation(target: object, outcome: object) -> str:
    """The operation identity a target or the outcome it got names."""
    named = getattr(outcome, "operation_id", None) or getattr(target, "invocation_id", None)
    return str(named if named is not None else target)


def _operation(target: object, outcome: object) -> str:
    """The operation identity a target or the outcome it got names."""
    named = getattr(outcome, "operation_id", None) or getattr(target, "invocation_id", None)
    return str(named if named is not None else target)


def _measurements(core: CoreState) -> dict[str, str]:
    """Each measurement request core holds, with how it stands."""
    return {
        row.request_id.root: f"{row.phase.value}, {row.observation.status.value if row.observation else 'unobserved'}"
        for row in core.records.records
        if row.request.kind == "submit_measurement"
    }


#: How a job lives on when its sbatch is faulted with this kind: submitted, then it dies (out
#: of memory, preemption, node failure), or it waits in the queue for good.
_SUBMITTED_FATE: dict[ClusterFault, tuple[SlurmJobStatus, ...]] = {
    ClusterFault.KILLED: (SlurmJobStatus.FAILED,),
    ClusterFault.PENDING_FOREVER: (SlurmJobStatus.PENDING,),
}
#: Faults that leave the call's answer unknown, as the real ``SlurmCluster`` reports a failed,
#: garbled, cut-off, or empty scheduler reading.
_UNREADABLE = frozenset(
    {
        ClusterFault.SSH_DOWN,
        ClusterFault.COMMAND_ERROR,
        ClusterFault.WRONG_STATE,
        ClusterFault.ACCOUNTING_GAP,
        ClusterFault.TRUNCATED,
    }
)


class FaultyCluster(ScenarioCluster):
    """The Fake cluster, with the plan's cluster faults struck at the calls they name.

    Calls are counted per Slurm operation as ``vs_faults`` counts connector requests: a submit is
    an ``sbatch``; an inspect is an ``squeue`` and an ``sacct``; a cancel is a ``scancel``; a
    collect is a transfer and an exec. A rule fires on the ``at``-th call of its operation. A
    faulted call fails as a failing cluster does: transport and command errors and a garbled
    answer give ``ClusterUnknown`` and reach nothing; ``killed`` lets the submission through, and
    its job then dies.
    """

    def __init__(self, plan: FaultPlan, ticks: Ticks) -> None:
        """Fault ``plan``'s cluster calls; every call is a tick of ``ticks``."""
        super().__init__()
        self._plan = plan
        self._ticks = ticks
        self._counts: Counter[ClusterOperation] = Counter()
        self.injected: list[tuple[ClusterOperation, int, ClusterFault]] = []
        self.settled: set[str] = set()
        """Operations the run saw end on the scheduler."""
        self.cancel_requested: set[str] = set()
        """Operations whose cancellation the scheduler acknowledged."""

    def _strike(self, *operations: ClusterOperation) -> ClusterFault | None:
        self._ticks.tick()
        struck: ClusterFault | None = None
        for operation in operations:
            self._counts[operation] += 1
            rule = self._plan.match(Boundary.CLUSTER, operation.value, self._counts[operation])
            if rule is not None and struck is None:
                struck = ClusterFault(str(rule.fault))
                self.injected.append((operation, self._counts[operation], struck))
        return struck

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        """Submit, or fail the call as the plan schedules."""
        fault = self._strike(ClusterOperation.SBATCH)
        if fault in {ClusterFault.SSH_DOWN, ClusterFault.COMMAND_ERROR}:
            return ClusterUnknown(operation_id=operation_id, reason=f"sbatch: {fault.value}")
        if fault is ClusterFault.TRUNCATED:
            # The job is accepted, but its id was cut off the reply.
            super().submit(request, operation_id=operation_id)
            return ClusterUnknown(operation_id=operation_id, reason="sbatch: truncated")
        fate = _SUBMITTED_FATE.get(fault) if fault is not None else None
        if fate is not None and operation_id not in self.submissions:
            alive, self.states = self.states, fate
            try:
                return super().submit(request, operation_id=operation_id)
            finally:
                self.states = alive
        return super().submit(request, operation_id=operation_id)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        """Observe the job, or fail the squeue or sacct that reads it."""
        fault = self._strike(ClusterOperation.SQUEUE, ClusterOperation.SACCT)
        if fault in _UNREADABLE:
            return self._unknown(target, by_job_id=by_job_id, reason=f"inspect: {fault.value}")
        outcome = super().inspect(target, by_job_id=by_job_id)
        if fault is ClusterFault.REQUEUED and outcome.kind == "observed":
            # The node failed and Slurm queued the job again; the next reading moves on. A
            # scheduler reading carries no attempt number, so the requeue reads as attempt 0.
            return outcome.model_copy(update={"life": Queued(attempt=outcome.life.attempt)})
        if outcome.kind == "observed" and isinstance(outcome.life, Ended):
            self.settled.add(_operation(target, outcome))
        return outcome

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        """Cancel the job, or fail the scancel."""
        fault = self._strike(ClusterOperation.SCANCEL)
        if fault in _UNREADABLE:
            return self._unknown(target, by_job_id=by_job_id, reason=f"scancel: {fault.value}")
        outcome = super().cancel(target, by_job_id=by_job_id)
        if outcome.kind == "cancel_requested":
            self.cancel_requested.add(_operation(target, outcome))
        return outcome

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCollectOutcome:
        """Collect the job's evidence, or fail the transfer or exec that reads it."""
        fault = self._strike(ClusterOperation.TRANSFER, ClusterOperation.EXEC)
        if fault in _UNREADABLE:
            return self._unknown(target, by_job_id=by_job_id, reason=f"collect: {fault.value}")
        outcome = super().collect(target, by_job_id=by_job_id, observed=observed)
        if outcome.kind == "collected":
            self.settled.add(_operation(target, outcome))
        return outcome

    @staticmethod
    def _unknown(target: ClusterTarget, *, by_job_id: bool, reason: str) -> ClusterUnknown:
        return ClusterUnknown(
            operation_id=None if by_job_id else str(target),
            job_id=str(target) if by_job_id else None,
            reason=reason,
        )

    def jobs(self) -> dict[str, str]:
        """Each submitted job and how the run left it (the ledger line ``cluster_jobs`` reads)."""
        return {
            operation: "CANCELLED"
            if operation in self.cancel_requested
            else "COMPLETED"
            if operation in self.settled
            else "RUNNING"
            for operation in self.submissions
        }


@dataclass
class FaultedHost(SessionHost):
    """The skeleton's session host, with the plan's agent faults between the shell and the agent."""

    faulty: FaultyAgentClient | None = None

    def sessions(self) -> FakeAgentSessions:
        """Journal-backed sessions whose turns go through the fault wrapper."""
        assert self.faulty is not None
        return FakeAgentSessions(self.faulty, self.journal, threads=loop_threads())


def _faulted_agents(plan: FaultPlan) -> Callable[[Path], SessionHost]:
    def open_agents(root: Path) -> SessionHost:
        writer = CandidateWriter(root)
        threads = loop_threads()
        client = AgentClient(
            FakeProvider(answer=writer.answer, on_turn=writer, threads=threads),
            clock=ManualClock(),
            threads=threads,
        )
        resolver = CandidateResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
        )
        return FaultedHost(
            resolver,
            client,
            FakeAgentInvocationStore(),
            [],
            ProviderFaults(),
            faulty=FaultyAgentClient(client, plan, threads=threads),
        )

    return open_agents


def plan_for(seed: int, *, faults: int | None = None) -> FaultPlan:
    """Return the fault plan seed ``seed`` runs under (0 to 4 faults)."""
    count = faults if faults is not None else seed % 5
    drawn = FaultPlan.generate(
        seed,
        targets={Boundary.AGENT_TURN: ROLES, Boundary.TOOL_CALL: (), Boundary.CLUSTER: ()},
        faults=count,
    )
    # A reply that names values the agent invented is judged by the search, which the skeleton
    # does not do (it takes the commit its implementer names), so it is no fault of the shell's.
    return drawn.model_copy(
        update={"rules": tuple(r for r in drawn.rules if r.fault is not AgentFault.WRONG_VALUES)}
    )


@dataclass
class ChaosRun:
    """One finished chaos run and every violation it showed."""

    seed: int
    plan: FaultPlan
    ending: str
    violations: list[tuple[str, str]]
    injected: list[str]
    suffered: Injected
    stop_at: int | None = None

    def summary(self) -> dict[str, object]:
        """Return a JSON-ready summary: ending, faults, violations."""
        return {
            "seed": self.seed,
            "ending": self.ending,
            "injected": self.injected,
            "stop_at": self.stop_at,
            "violations": [str(item) for item in self.violations],
        }

    def report(self) -> str:
        """Return the failure report: seed, repro, injected faults, violations."""
        lines = [
            f"seed {self.seed}: {len(self.violations)} violation(s); {self.ending}",
            repro(self.seed),
        ]
        lines += [f"  injected: {item}" for item in self.injected]
        if self.stop_at is not None:
            lines.append(f"  stop requested at tick {self.stop_at}")
        lines += [f"  VIOLATION {item}" for item in self.violations]
        return "\n".join(lines)


class PastBudgetError(Exception):
    """The run's virtual time passed its budget: it is stuck, so the harness stops it."""

    def __init__(self, now: float, budget_s: float) -> None:
        super().__init__(f"virtual time {now:.1f} s passed the {budget_s:.0f} s budget")
        self.now = now


class _TickingClock(ProbedRunClock):
    """The run clock; each wait of the loop is a tick, and no wait may pass the budget.

    A stuck run that keeps waiting would otherwise advance virtual time forever, costing real
    time until the test's own timeout. Ending it at the budget makes a hang fail in seconds.
    """

    def __init__(self, inner: VirtualClock, ticks: Ticks, budget_s: float) -> None:
        super().__init__(inner)
        self._ticks = ticks
        self._budget_s = budget_s

    async def sleep(self, seconds: float) -> None:
        """Wait, then count the wait; raise ``PastBudgetError`` once the wait passes the budget."""
        await super().sleep(seconds)
        self._ticks.tick()
        if self.now() > self._budget_s:
            raise PastBudgetError(self.now(), self._budget_s)


class _NoSteers:
    def put(self, ref: object, text: str) -> None:
        del ref, text


def _ignore(transition: object) -> None:
    del transition


def shape_for(seed: int) -> SkeletonConfig:
    """The skeleton a seed plays: one to three turns in a row (peers: see ``test_peer_failure``)."""
    rng = FaultPlan(seed=seed).rng("shape")
    return SkeletonConfig(turns=rng.randint(1, 3))


async def run_chaos(  # noqa: PLR0913  # LW-150102 [PLR0913]; the options are independent and keyword-only with defaults.
    # > Bundling stop_at, config and budget_s into an options object would make every one of
    # > the nine callers build it to set a single option, without adding meaning.
    base: Path,
    seed: int,
    plan: FaultPlan | None = None,
    *,
    stop_at: int | None | str = "seeded",
    config: SkeletonConfig | None = None,
    budget_s: float = BUDGET_S,
) -> ChaosRun:
    """Run the skeleton once under ``seed``'s faults and stop, and check every invariant.

    A run whose virtual time passes ``budget_s`` is stopped there and reported as a hang.
    """
    plan = plan if plan is not None else plan_for(seed)
    rng = plan.rng("options")
    if stop_at == "seeded":
        stop_at = rng.choice((None, None, rng.randint(1, _MAX_STOP_TICK)))
    assert stop_at is None or isinstance(stop_at, int)
    channel = RuntimeRunControlChannel(_ignore, threads=loop_threads())
    ticks = Ticks(stop_at, channel)
    cluster = FaultyCluster(plan, ticks)
    strategy = SkeletonStrategy(config=config or shape_for(seed))
    clock = _TickingClock(clock_from(1.0), ticks, budget_s)
    outcome: RunOutcome | None = None
    error: BaseException | None = None
    hung: PastBudgetError | None = None
    violations: list[tuple[str, str]] = []
    with open_skeleton_world(base, strategy, cluster, agents=_faulted_agents(plan)) as world:
        turns = strategy.config.turns + strategy.config.peers
        world.limits = sample_limits(max_turns=4 * turns + 4, reconciliation_bound=300.0)
        world.job_clock = clock
        process = world.runtime()
        host = CoreRunHost(
            process.shell, process.delivery, clock, RunControlBridge(channel, _NoSteers())
        )  # type: ignore[arg-type]
        process.shell.start("chaos", now_at=1.0, lease_duration=LEASE)
        loop = RunLoopConfig(
            host_id="chaos",
            lease_duration=LEASE,
            control_poll_interval=1.0,
            min_sleep=0.05,
            max_dispatches=_MAX_DISPATCHES,
        )
        try:
            outcome = await drive_core(host, loop)
        except PastBudgetError as stuck:
            hung = stuck
        except Exception as raised:  # noqa: BLE001  # lint-waiver: LW-150100 [BLE001]; whatever the run raises is its ending, judged by the oracle.
            error = raised
        core = process.shell.record.envelope.core
        for evaluation in world.evaluations:
            await evaluation.close()
        faulty = world.agents.faulty if isinstance(world.agents, FaultedHost) else None
        assert faulty is not None
    suffered = Injected(
        agent=frozenset(item[2] for item in faulty.injected),
        cluster=frozenset(item[2] for item in cluster.injected),
        stop_delivered=ticks.stop_delivered,
    )
    injected = [str(item) for item in faulty.injected] + [str(item) for item in cluster.injected]
    if hung is not None:
        # The run never ended, so there is no ending to judge: report what it waits on.
        open_waits = ", ".join(f"{wait.waiter}:{wait.subject}" for wait in waits(core))
        ended = repr(hung)
        violations.append((ChaosInvariant.HANG, f"{hung}; still waiting on: {open_waits}"))
    else:
        if clock.now() > budget_s:
            violations.append((ChaosInvariant.HANG, f"run took {clock.now()} s > {budget_s} s"))
        ended, judged = _judge_ending(outcome, error, suffered, core, cluster)
        violations += judged
    chaos = ChaosRun(seed, plan, ended, violations, injected, suffered, stop_at)
    _log(chaos)
    return chaos


def _judge_ending(
    outcome: RunOutcome | None,
    error: BaseException | None,
    suffered: Injected,
    core: CoreState,
    cluster: FaultyCluster,
) -> tuple[str, list[tuple[str, str]]]:
    """How a run that ended ended, and the violations its ending and record show."""
    violations: list[tuple[str, str]] = []
    end_error, status = ending_of(outcome, error)
    reason = outcome.result.reason if outcome is not None and outcome.result is not None else ""
    ended = repr(end_error) if end_error is not None else f"{status} ({reason})"
    if (why := unexplained_end(end_error, status, suffered)) is not None:
        violations.append((ChaosInvariant.UNEXPLAINED_END, f"{why}: {ended}"))
    violations += _record_violations(core, cluster, end_error, status)
    return ended, violations


def _log(chaos: ChaosRun) -> None:
    """Append the run's summary to ``$CHAOS_LOG`` when it is set."""
    log = os.environ.get("CHAOS_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(chaos.summary()) + "\n")


def _record_violations(
    core: CoreState,
    cluster: FaultyCluster,
    error: BaseException | None,
    status: RunStatus | None,
) -> list[tuple[str, str]]:
    """Check the shared invariants that apply, core liveness and the stop rule on what is left."""
    found: list[tuple[str, str]] = []
    if error is None:
        found += [
            (violation.invariant.value, violation.detail)
            for violation in cluster_jobs(RunRecords(events=(), cluster_jobs=cluster.jobs()))
        ]
        end = End.STOPPED if status is RunStatus.STOPPED else End.TERMINAL
        retries = core.run.limits.request_retries.limit
        found += [
            (ChaosInvariant.UNLIVE, str(item))
            for item in liveness(Journal.from_ledger(core), core, Budget(retries=retries), end)
        ]
    return found
