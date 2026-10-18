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
    LedgerLabels,
    LedgerPolicy,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmSshTransport,
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
# How often a caller tries again after an unknown outcome, as the executor does.
_TRIES = 4

Fault = Literal["fails", "answer_lost"]


@dataclass
class _Steps:
    """Counts the calls one actor makes, and runs the other actor whole before the ``at``-th."""

    at: int
    faults: dict[int, Fault] = field(default_factory=dict)
    count: int = 0
    armed: bool = False
    intruder: Callable[[], object] | None = None

    def step(self) -> Fault | None:
        if not self.armed:
            return None
        self.count += 1
        if self.count == self.at and self.intruder is not None:
            self.armed = False  # the intruder's own calls go straight through
            try:
                self.intruder()
            finally:
                self.armed = True
        return self.faults.get(self.count)


@dataclass
class _SteppedLedger:
    """The Fake ledger, with every call of the stepped actor counted."""

    inner: FakeEvaluationLedger
    steps: _Steps

    def read(self, key: str) -> LedgerEntry | None:
        self.steps.step()
        return self.inner.read(key)

    def claim(self, record: LedgerRecord) -> LedgerRecord:
        self.steps.step()
        return self.inner.claim(record)

    def replace(self, current: LedgerEntry, record: LedgerRecord) -> bool:
        self.steps.step()
        return self.inner.replace(current, record)

    def renew(self, key: str, lease: LedgerLease) -> None:
        self.steps.step()
        self.inner.renew(key, lease)

    def release(self, key: str, holder: str) -> None:
        self.steps.step()
        self.inner.release(key, holder)

    def list_keys(self) -> tuple[str, ...]:
        self.steps.step()
        return self.inner.list_keys()

    def operator_file_sha256(self, path: str) -> str | None:
        return self.inner.operator_file_sha256(path)


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
        self.steps.step()
        return self.inner.submit(request, operation_id=operation_id)

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        fault = self.steps.step()
        if fault == "fails":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        observed = self.inner.inspect(target, by_job_id=by_job_id)
        if fault == "answer_lost":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        return observed

    def cancel(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterCancelOutcome:
        fault = self.steps.step()
        if fault == "fails":
            return ClusterUnknown(operation_id=None, reason="ssh: connection reset")
        cancelled = self.inner.cancel(target, by_job_id=by_job_id)
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
        self.cluster = FakeCluster(clock=self.clock)
        self.ledger = FakeEvaluationLedger(project=PROJECT)
        self.steps = steps
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
    run: SlurmLedgerAttachment, cluster: FakeCluster | _SteppedCluster, request: SlurmBatchRequest
) -> Attached:
    """Attach, asking again while another process holds the request, as the executor does."""
    for _ in range(_TRIES):
        outcome = run.attach(cluster, request, labels=LedgerLabels())
        if isinstance(outcome, Attached):
            return outcome
    message = f"the request stayed pending: {outcome}"
    raise AssertionError(message)


def _relinquish_until_known(
    run: SlurmLedgerAttachment, cluster: FakeCluster | _SteppedCluster, key: str
) -> None:
    """Stop wanting the request, trying again after an unknown outcome, as the executor does."""
    for _ in range(_TRIES):
        if not isinstance(run.relinquish(cluster, key), WithdrawUnknown):
            return
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
        held.append(_attach_until_attached(second, world.cluster, request))
        assert world.live(held[0].operation_id)
    assert len(world.live_jobs(held[0].key)) == 1


_FAULTS = st.dictionaries(
    st.integers(min_value=1, max_value=14), st.sampled_from(["fails", "answer_lost"]), max_size=2
)


@pytest.mark.xfail(
    strict=True, reason="#3173 review finding 1: a cross-run attach races a withdrawal"
)
@settings(max_examples=examples(pr=150, nightly=1500), deadline=None)
@given(
    race=st.sampled_from(
        [
            "attach_during_relinquish",
            "relinquish_during_attach",
            "sweep_during_attach",
            "attach_during_sweep",
            "relinquish_during_relinquish",
        ]
    ),
    at=st.integers(min_value=1, max_value=14),
    faults=_FAULTS,
)
# The reviewed race: another run attaches while the relinquishing run cancels the job.
@example(race="attach_during_relinquish", at=2, faults={})
@example(race="attach_during_relinquish", at=3, faults={})
# A relinquish between an attacher's read of the record and its lease write.
@example(race="relinquish_during_attach", at=2, faults={})
@example(race="relinquish_during_attach", at=3, faults={})
def test_no_withdrawal_cancels_the_job_of_a_live_run_and_no_job_outlives_every_run(
    race: Race, at: int, faults: dict[int, Fault]
) -> None:
    steps = _Steps(at=at, faults=faults)
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
        _attach_until_attached(first, steps_off, request_1)
        held_by_second: list[Attached] = []
        if race == "relinquish_during_relinquish":
            held_by_second.append(_attach_until_attached(second, steps_off, request_2))
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
                lambda: _relinquish_until_known(first, steps_off, key),
            ),
            "sweep_during_attach": (
                lambda: second.attach(world.stepped_cluster, request_2, labels=LedgerLabels()),
                lambda: sweeper.sweep_if_due(steps_off),
            ),
            "attach_during_sweep": (
                lambda: sweeper.sweep_if_due(world.stepped_cluster),
                attach_second,
            ),
            "relinquish_during_relinquish": (
                lambda: first.relinquish(world.stepped_cluster, key),
                lambda: _relinquish_until_known(second, steps_off, key),
            ),
        }
        stepped, intruder = actions[race]
        steps.intruder = intruder
        steps.armed = True
        outcome = stepped()
        steps.armed = False
        if steps.count < at and steps.intruder is not None:
            intruder()  # the stepped actor made fewer calls: the intruder runs after it
        if isinstance(outcome, Attached):
            held_by_second.append(outcome)

        # Whatever ended unknown is tried again, as the executor does.
        if race in {"attach_during_relinquish", "relinquish_during_relinquish"}:
            _relinquish_until_known(first, steps_off, key)
        if race == "relinquish_during_relinquish":
            assert world.live_jobs(key) == [], "a job outlived both runs that let go of it"
            return

        _check_the_live_run_keeps_its_job(world, second, request_2, held_by_second)
        _relinquish_until_known(second, steps_off, key)
        if race == "attach_during_relinquish":
            _relinquish_until_known(first, steps_off, key)
        # A run that crashed (the sweeps) lets go of nothing: its lapsed lease holds nothing.
        assert world.live_jobs(key) == [], "a job outlived every run that wanted it"
