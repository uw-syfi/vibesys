"""Two runs on one ledger request: a withdrawal never cancels a job a live run attached to.

A run that stops wanting a request withdraws it (``relinquish``) unless another
run wants it; a sweep withdraws one no run has leased for the grace period. Each
is several ledger and cluster calls, and so is an attach. Leases are separate
files from the record, so nothing makes those calls atomic: here a second actor
runs whole between any two calls of the first, in both orders, with the cluster
failing or losing the answer of some calls.

After both actors, a run that is still live must hold a job that is not
cancelled, and once every run let go of the request no job may be left queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import Attached, SlurmLedgerAttachment
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterObservation,
    ClusterUnknown,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerError,
    LedgerLabels,
    LedgerPolicy,
    SecondsRange,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmSshTransport,
    SlurmTimingProfile,
    WithdrawalTiming,
    WithdrawUnknown,
    ledger_operation_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import ByteSource, OutputStream
    from vs_slurm.api import (
        ClusterCancelOutcome,
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        ClusterSubmitOutcome,
        ClusterTarget,
        LedgerEntry,
        LedgerLease,
        LedgerRecord,
        SlurmBatchHandle,
        SlurmJobRequest,
        WithdrawalMark,
        WithdrawalUndone,
    )
    from vs_slurm.api import (
        ClusterObservation as Observation,
    )

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
)
POLICY = LedgerPolicy()
_QUEUED_FOREVER = SlurmTimingProfile(queue_wait_s=SecondsRange.exactly(10**9))
# How often a caller tries again after an unknown outcome, as the executor does.
_TRIES = 4
# The pause before each try of an attach: the last one outlasts a withdrawal's window.
_WINDOW = WithdrawalTiming(
    clock=ManualClock(), call_seconds=CONFIG.transport_timeout_seconds
).window_seconds
_ATTACH_PAUSES = (0.0, 0.0, 0.0, _WINDOW + 1.0)

Fault = Literal["fails", "answer_lost"]


@dataclass
class _Steps:
    """Counts the calls one actor makes, and runs the other actor whole before the ``at``-th.

    Each counted call takes ``seconds`` of the ``clock``. ``faults`` reach the
    cluster's inspect and cancel; with ``every_call_fails`` they also reach its
    submit and every ledger call.
    """

    at: int
    faults: dict[int, Fault] = field(default_factory=dict)
    count: int = 0
    armed: bool = False
    intruder: Callable[[], object] | None = None
    clock: ManualClock | None = None
    seconds: float = 0.0
    every_call_fails: bool = False

    def step(self) -> Fault | None:
        if not self.armed:
            return None
        if self.clock is not None:
            self.clock.advance(self.seconds)
        self.count += 1
        if self.count == self.at and self.intruder is not None:
            self.armed = False  # the intruder's own calls go straight through
            try:
                self.intruder()
            finally:
                self.armed = True
        return self.faults.get(self.count)

    def ignore(self) -> None:
        """Count a call that no fault reaches."""
        self.step()


@dataclass
class _SteppedLedger:
    """The Fake ledger, with every call of the stepped actor counted.

    With ``every_call_fails``, a call that ``fails`` never reaches the ledger,
    and one whose ``answer_lost`` reaches it while its caller hears an error.
    """

    inner: FakeEvaluationLedger
    steps: _Steps

    def _call[R](self, call: Callable[[], R]) -> R:
        fault = self.steps.step() if self.steps.every_call_fails else self.steps.ignore()
        if fault == "fails":
            raise LedgerError.transport(ConnectionResetError("ssh: connection reset"))
        answer = call()
        if fault == "answer_lost":
            raise LedgerError.transport(ConnectionResetError("ssh: connection reset"))
        return answer

    def read(self, key: str) -> LedgerEntry | None:
        return self._call(lambda: self.inner.read(key))

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        return self._call(lambda: self.inner.claim(record))

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        return self._call(lambda: self.inner.replace(current, record))

    def write_withdrawal(
        self, current: LedgerEntry, document: WithdrawalMark | WithdrawalUndone
    ) -> bool:
        return self._call(lambda: self.inner.write_withdrawal(current, document))

    def renew(self, key: str, lease: LedgerLease) -> None:
        self._call(lambda: self.inner.renew(key, lease))

    def release(self, key: str, holder: str) -> None:
        self._call(lambda: self.inner.release(key, holder))

    def list_keys(self) -> tuple[str, ...]:
        return self._call(self.inner.list_keys)

    def operator_file_sha256(self, path: str) -> str | None:
        return self.inner.operator_file_sha256(path)

    def bind_handle(self, holder: str, handle: str, key: str) -> str:
        return self.inner.bind_handle(holder, handle, key)

    def bound_key(self, holder: str, handle: str) -> str | None:
        return self.inner.bound_key(holder, handle)


@dataclass
class _SteppedCluster:
    """The Fake cluster, with every call of the stepped actor counted and some failing.

    A call that ``fails`` never reaches the scheduler; one whose ``answer_lost``
    reaches it, and its caller hears nothing.
    """

    inner: FakeCluster
    steps: _Steps

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        fault = self.steps.step() if self.steps.every_call_fails else self.steps.ignore()
        if fault == "fails":
            return ClusterUnknown(operation_id=operation_id, reason="ssh: connection reset")
        submitted = self.inner.submit(request, operation_id=operation_id)
        if fault == "answer_lost":
            return ClusterUnknown(operation_id=operation_id, reason="ssh: connection reset")
        return submitted

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        fault = self.steps.step()
        if fault == "fails":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        observed = self.inner.inspect(target, by_job_id=by_job_id)
        if fault == "answer_lost":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        return observed

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        fault = self.steps.step()
        if fault == "fails":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        cancelled = self.inner.cancel(target, by_job_id=by_job_id, observed=observed)
        if fault == "answer_lost":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        return cancelled

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: Observation | None = None,
    ) -> ClusterCollectOutcome:
        return self.inner.collect(target, by_job_id=by_job_id, observed=observed)

    def stage_output(
        self, handle: SlurmBatchHandle, index: int, stream: OutputStream
    ) -> ByteSource:
        return self.inner.stage_output(handle, index, stream)


Race = Literal[
    # A run that attaches while another run relinquishes, between any two of its calls.
    "attach_during_relinquish",
    # A run that relinquishes while another run attaches, between any two of its calls.
    "relinquish_during_attach",
    # A sweep that runs while a run attaches to a request whose last lease lapsed.
    "sweep_during_attach",
    # A run that attaches while a sweep withdraws a request whose last lease lapsed.
    "attach_during_sweep",
    # Two runs that both stop wanting the request at once.
    "relinquish_during_relinquish",
]


class _World:
    def __init__(self, root: Path, steps: _Steps) -> None:
        self.root = root
        self.clock = ManualClock(1000.0)
        # Every job queues until it is cancelled: a job that ends on its own wants nothing.
        self.cluster = FakeCluster(clock=self.clock, timing=_QUEUED_FOREVER)
        self.ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())
        self.steps = steps
        steps.clock = self.clock
        self.stepped_cluster = _SteppedCluster(self.cluster, steps)

    def run(self, holder: str, *, stepped: bool = False) -> SlurmLedgerAttachment:
        ledger = self.ledger.reopen()
        return SlurmLedgerAttachment(
            ledger=_SteppedLedger(ledger, self.steps) if stepped else ledger,
            config=CONFIG,
            project=PROJECT,
            holder=holder,
            policy=POLICY,
            clock=self.clock,
        )

    def request(self, run: str) -> SlurmBatchRequest:
        workspace = self.root / run / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bench.py").write_text("print('bench')", encoding="utf-8")
        return SlurmBatchRequest(
            workspace=workspace,
            stages=(SlurmBatchStage(name="benchmark", command=("python", "bench.py")),),
        )

    def live(self, operation: str) -> bool:
        observed = self.cluster.inspect(operation)
        return isinstance(observed, ClusterObservation) and not observed.status.terminal

    def live_jobs(self, key: str) -> list[str]:
        entry = self.ledger.read(key)
        attempts = 0 if entry is None else entry.record.attempt + 1
        operations = (ledger_operation_id(key, attempt) for attempt in range(attempts))
        return [operation for operation in operations if self.live(operation)]


def _attach_until_attached(
    run: SlurmLedgerAttachment,
    cluster: FakeCluster | _SteppedCluster,
    request: SlurmBatchRequest,
    clock: ManualClock,
) -> Attached:
    """Attach, asking again while another process holds the request, as the executor does.

    The executor asks again after a pause, for as long as the evaluation may
    take: the last try here comes after a withdrawal's whole window, which a
    mark left by a withdrawer that failed part way holds the request for.
    """
    for pause in _ATTACH_PAUSES:
        clock.advance(pause)
        outcome = run.attach(cluster, request, labels=LedgerLabels())
        if isinstance(outcome, Attached):
            return outcome
    message = f"the request stayed pending: {outcome}"
    raise AssertionError(message)


def _relinquish_until_known(
    run: SlurmLedgerAttachment, cluster: FakeCluster | _SteppedCluster, key: str, clock: ManualClock
) -> None:
    """Stop wanting the request, trying again after an unknown outcome, as the executor does.

    The executor tries again at a later cancel or stop. Only another
    withdrawer's mark, left by one that failed part way, may hold the request
    for its whole window; a mark of this run's own is its to finish at once.
    """
    for _ in range(_TRIES):
        outcome = run.relinquish(cluster, key)
        if not isinstance(outcome, WithdrawUnknown):
            return
        withdrawer = outcome.reason.removesuffix(" is withdrawing it")
        if withdrawer not in {outcome.reason, run.holder}:
            clock.advance(_WINDOW + 1.0)
    message = "the withdrawal stayed unknown"
    raise AssertionError(message)


def _check_the_live_run_keeps_its_job(
    world: _World,
    second: SlurmLedgerAttachment,
    request: SlurmBatchRequest,
    held: list[Attached],
) -> None:
    """The second run is live: the job it holds, now or once it asks again, runs."""
    for attached in held:
        assert world.live(attached.operation_id), (
            f"run-2 attached to {attached.operation_id}, whose job was then cancelled"
        )
    if not held:
        held.append(_attach_until_attached(second, world.cluster, request, world.clock))
        assert world.live(held[0].operation_id)
    assert len(world.live_jobs(held[0].key)) == 1


_FAULTS = st.dictionaries(
    st.integers(min_value=1, max_value=14), st.sampled_from(["fails", "answer_lost"]), max_size=2
)
_RACES: tuple[Race, ...] = (
    "attach_during_relinquish",
    "relinquish_during_attach",
    "sweep_during_attach",
    "attach_during_sweep",
    "relinquish_during_relinquish",
)


def _quietly(action: Callable[[], object]) -> object:
    """Run one actor's call as the executor does: an unreachable ledger is an unknown outcome."""
    try:
        return action()
    except LedgerError:
        return None


def _race(race: Race, steps: _Steps) -> None:
    """Run the race; a live run keeps its job, and no job outlives every run that wanted it."""
    at = steps.at
    with TemporaryDirectory() as directory:
        world = _World(Path(directory), steps)
        first = world.run(
            "run-1", stepped=race in {"attach_during_relinquish", "relinquish_during_relinquish"}
        )
        second = world.run("run-2", stepped=race.endswith("during_attach"))
        sweeper = world.run("run-3", stepped=race == "attach_during_sweep")
        request_1, request_2 = world.request("run-1"), world.request("run-2")
        key = first.key(request_1)
        steps_off = world.cluster
        _attach_until_attached(first, steps_off, request_1, world.clock)
        held_by_second: list[Attached] = []
        if race == "relinquish_during_relinquish":
            held_by_second.append(_attach_until_attached(second, steps_off, request_2, world.clock))
        if race in {"sweep_during_attach", "attach_during_sweep"}:
            # The first run crashed long ago: its lease lapsed past the grace period.
            world.clock.advance(POLICY.lease_seconds + POLICY.grace_seconds + 1)

        def attach_second() -> None:
            outcome = second.attach(steps_off, request_2, labels=LedgerLabels())
            if isinstance(outcome, Attached):
                held_by_second.append(outcome)

        actions: dict[str, tuple[Callable[[], object], Callable[[], object]]] = {
            # (the stepped actor, the intruder)
            "attach_during_relinquish": (
                lambda: first.relinquish(world.stepped_cluster, key),
                attach_second,
            ),
            "relinquish_during_attach": (
                lambda: second.attach(world.stepped_cluster, request_2, labels=LedgerLabels()),
                lambda: _relinquish_until_known(first, steps_off, key, world.clock),
            ),
            "sweep_during_attach": (
                lambda: second.attach(world.stepped_cluster, request_2, labels=LedgerLabels()),
                lambda: sweeper.sweep_if_due(steps_off),
            ),
            "attach_during_sweep": (
                lambda: _quietly(lambda: sweeper.sweep_if_due(world.stepped_cluster)),
                attach_second,
            ),
            "relinquish_during_relinquish": (
                lambda: first.relinquish(world.stepped_cluster, key),
                lambda: _relinquish_until_known(second, steps_off, key, world.clock),
            ),
        }
        stepped, intruder = actions[race]
        steps.intruder = intruder
        steps.armed = True
        outcome = _quietly(stepped)
        steps.armed = False
        if steps.count < at and steps.intruder is not None:
            intruder()  # the stepped actor made fewer calls: the intruder runs after it
        if isinstance(outcome, Attached):
            held_by_second.append(outcome)

        # Whatever ended unknown is tried again, as the executor does.
        if race in {"attach_during_relinquish", "relinquish_during_relinquish"}:
            _relinquish_until_known(first, steps_off, key, world.clock)
        if race == "relinquish_during_relinquish":
            assert world.live_jobs(key) == [], "a job outlived both runs that let go of it"
            return

        _check_the_live_run_keeps_its_job(world, second, request_2, held_by_second)
        _relinquish_until_known(second, steps_off, key, world.clock)
        if race == "attach_during_relinquish":
            _relinquish_until_known(first, steps_off, key, world.clock)
        # A run that crashed (the sweeps) lets go of nothing: its lapsed lease holds nothing.
        assert world.live_jobs(key) == [], "a job outlived every run that wanted it"


@settings(max_examples=examples(pr=150, nightly=1500), deadline=None)
@given(race=st.sampled_from(_RACES), at=st.integers(min_value=1, max_value=14), faults=_FAULTS)
# The reviewed race: another run attaches while the relinquishing run cancels the job.
@example(race="attach_during_relinquish", at=2, faults={})
@example(race="attach_during_relinquish", at=3, faults={})
# A relinquish between an attacher's read of the record and its lease write.
@example(race="relinquish_during_attach", at=2, faults={})
@example(race="relinquish_during_attach", at=3, faults={})
def test_no_withdrawal_cancels_the_job_of_a_live_run_and_no_job_outlives_every_run(
    race: Race, at: int, faults: dict[int, Fault]
) -> None:
    _race(race, _Steps(at=at, faults=faults))


# Seconds each remote call of the stepped actor takes: none, a slow link, and the
# default transport timeout, which a live but slow withdrawer may spend on every call.
_CALL_SECONDS = st.sampled_from([0.0, 37.0, 110.0])


@pytest.mark.regression
@settings(max_examples=examples(pr=150, nightly=1500), deadline=None)
@given(
    race=st.sampled_from(_RACES),
    at=st.integers(min_value=1, max_value=14),
    faults=_FAULTS,
    seconds=_CALL_SECONDS,
)
# MED-2: 110 s calls; run-2 attaches after the mark expired, then loses its job.
@example(race="attach_during_relinquish", at=7, faults={}, seconds=110.0)
@example(race="attach_during_relinquish", at=8, faults={}, seconds=110.0)
# LOW-5: the answer to the withdrawal's mark write is lost.
@example(race="attach_during_relinquish", at=15, faults={5: "answer_lost"}, seconds=0.0)
def test_no_withdrawal_cancels_a_live_runs_job_however_slow_or_faulty_its_calls(
    race: Race, at: int, faults: dict[int, Fault], seconds: float
) -> None:
    """r3173 MED-2, LOW-5, LOW-6: time passes on every call, and any call may fail.

    Every remote call of the stepped actor takes ``seconds`` of the shared clock,
    so marks and claims expire while it works; and its ledger calls and submits
    fail, or lose their answer, as well as its inspects and cancels.
    """
    _race(race, _Steps(at=at, faults=faults, seconds=seconds, every_call_fails=True))


@dataclass
class _FlakyCluster:
    """The Fake cluster, where the calls ``faults`` names fail or lose their answer.

    ``faults`` maps each kind of call to the fate of its first calls, in order.
    """

    inner: FakeCluster
    faults: dict[str, list[Fault | None]]

    def _fate(self, call: str) -> Fault | None:
        planned = self.faults.get(call, [])
        return planned.pop(0) if planned else None

    def submit(
        self, request: SlurmBatchRequest | SlurmJobRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        fate = self._fate("submit")
        if fate == "fails":
            return ClusterUnknown(operation_id=operation_id, reason="ssh: connection reset")
        submitted = self.inner.submit(request, operation_id=operation_id)
        if fate == "answer_lost":
            return ClusterUnknown(operation_id=operation_id, reason="ssh: connection reset")
        return submitted

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        fate = self._fate("inspect")
        if fate == "fails":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        observed = self.inner.inspect(target, by_job_id=by_job_id)
        if fate == "answer_lost":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        return observed

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        fate = self._fate("cancel")
        if fate == "fails":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        cancelled = self.inner.cancel(target, by_job_id=by_job_id, observed=observed)
        if fate == "answer_lost":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        return cancelled

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: Observation | None = None,
    ) -> ClusterCollectOutcome:
        return self.inner.collect(target, by_job_id=by_job_id, observed=observed)

    def stage_output(
        self, handle: SlurmBatchHandle, index: int, stream: OutputStream
    ) -> ByteSource:
        return self.inner.stage_output(handle, index, stream)


_FATES = st.lists(st.sampled_from([None, "fails", "answer_lost"]), max_size=4)
# Between two asks: none, the executor's poll pause, and past the claim window.
_PAUSES = st.lists(st.sampled_from([0.0, 1.0, POLICY.claim_seconds + 1.0]), min_size=1, max_size=6)


@pytest.mark.regression
@settings(max_examples=examples(pr=100, nightly=1000), deadline=None)
@given(submits=_FATES, inspects=_FATES, cancels=_FATES, pauses=_PAUSES)
# A lost sbatch answer, then a failed inspect, then a failed scancel.
@example(submits=["answer_lost"], inspects=["fails"], cancels=["fails"], pauses=[0.0, 0.0])
def test_one_run_never_holds_two_live_attempts_of_one_request(
    submits: list[Fault | None],
    inspects: list[Fault | None],
    cancels: list[Fault | None],
    pauses: list[float],
) -> None:
    """r3173 MED-3: one process asks for a request again after its submission's answer was lost.

    Its own claim is not a dead process's: whatever the cluster answers, and
    however long it waits, at most one attempt of the request has a live job.
    """
    with TemporaryDirectory() as directory:
        world = _World(Path(directory), _Steps(at=0))
        run = world.run("run-1")
        request = world.request("run-1")
        key = run.key(request)
        cluster = _FlakyCluster(
            world.cluster, {"submit": submits, "inspect": inspects, "cancel": cancels}
        )
        for pause in pauses:
            world.clock.advance(pause)
            run.attach(cluster, request, labels=LedgerLabels())
            assert len(world.live_jobs(key)) <= 1, f"live jobs: {world.live_jobs(key)}"
