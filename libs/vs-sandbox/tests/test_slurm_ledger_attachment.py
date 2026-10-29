"""A run's ledger attachment across the crash windows of a submission (#2757).

A submission is three steps: claim the request's attempt, sbatch it under a
name derived from the key, record the job. A process that dies after any step
leaves a state the next asker must finish, never duplicate.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    Attached,
    AttachPending,
    LedgerWithdrawnError,
    SlurmLedgerAttachment,
)
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    ClusterCall,
    ClusterCancelRequested,
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    LedgerLabels,
    LedgerPolicy,
    LedgerRecord,
    LedgerState,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchResult,
    SlurmBatchStage,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmError,
    SlurmFileArtifact,
    SlurmJobStatus,
    SlurmSshTransport,
    SlurmSubmissionRejectedError,
    SlurmTreeArtifact,
    WithdrawalTiming,
    Withdrawer,
    job_of_handle,
    ledger_operation_id,
    transitioned,
    withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_slurm.api import ClusterInspectOutcome, ClusterTarget

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="fake-cluster",
    remote_workspace_root="/runs",
    transport=SlurmSshTransport(host="fake-cluster"),
)


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = ManualClock(1000.0)
        self.cluster = FakeCluster(clock=self.clock)
        self.ledger = FakeEvaluationLedger(project=PROJECT, clock=ManualClock())

    def attachment(self, holder: str) -> SlurmLedgerAttachment:
        return SlurmLedgerAttachment(
            ledger=self.ledger.reopen(),
            config=CONFIG,
            project=PROJECT,
            holder=holder,
            policy=LedgerPolicy(),
            clock=self.clock,
        )

    def request(self, run: str, artifacts: tuple[str, ...] = ()) -> SlurmBatchRequest:
        """The benchmark request, from this run's own worktree, declaring ``artifacts``.

        A name ending in ``/`` is a tree artifact, any other a file artifact.
        """
        workspace = self.root / run / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bench.py").write_text("print('bench')", encoding="utf-8")
        return SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="benchmark",
                    command=("python", "bench.py"),
                    file_artifacts=tuple(
                        SlurmFileArtifact(
                            remote_path=name, local_path=workspace / name, collect_on_failure=True
                        )
                        for name in artifacts
                        if not name.endswith("/")
                    ),
                    tree_artifacts=tuple(
                        SlurmTreeArtifact(
                            remote_path=name.rstrip("/"), local_path=workspace / name.rstrip("/")
                        )
                        for name in artifacts
                        if name.endswith("/")
                    ),
                ),
            ),
        )

    def claim(self, holder: str, key: str, attempt: int = 0) -> LedgerRecord:
        record = LedgerRecord(
            key=key,
            project=PROJECT,
            attempt=attempt,
            operation_id=ledger_operation_id(key, attempt),
            owner=holder,
            created_at=self.clock.now(),
            claim_expires_at=self.clock.now() + LedgerPolicy().claim_seconds,
            labels=LedgerLabels(),
            state=LedgerState.SUBMITTING,
        )
        assert self.ledger.claim(record) == record
        return record

    def state(self, key: str) -> LedgerState:
        entry = self.ledger.read(key)
        assert entry is not None
        return entry.record.state


_ARTIFACTS = st.lists(
    st.sampled_from(("result.json", "metrics.txt", "traces/", "profile/")), unique=True, max_size=3
).map(tuple)


@given(crash_after=st.sampled_from(("nothing", "claim", "sbatch", "record")), artifacts=_ARTIFACTS)
@settings(max_examples=examples(pr=12, nightly=60), deadline=None)
def test_whichever_step_a_submitter_died_after_the_next_asker_gets_one_job(
    crash_after: str, artifacts: tuple[str, ...]
) -> None:
    """The next asker runs from another worktree; the job's artifacts land in its own."""
    with TemporaryDirectory() as directory:
        world = _World(Path(directory))
        dead = world.attachment("run-dead")
        key = dead.key(world.request("run-dead", artifacts))
        operation = ledger_operation_id(key, 0)
        if crash_after == "record":
            assert isinstance(
                dead.attach(
                    world.cluster, world.request("run-dead", artifacts), labels=LedgerLabels()
                ),
                Attached,
            )
        elif crash_after in {"claim", "sbatch"}:
            world.claim("run-dead", key)
            if crash_after == "sbatch":
                submitted = world.cluster.submit(
                    world.request("run-dead", artifacts), operation_id=operation
                )
                assert isinstance(submitted, ClusterSubmitted)

        attached = world.attachment("run-next").attach(
            world.cluster, world.request("run-next", artifacts), labels=LedgerLabels()
        )
        if crash_after == "claim":
            # No job under the claim's name: the asker waits for the claim to expire,
            # then takes it over with a new attempt, without any other run's help.
            assert isinstance(attached, AttachPending)
            world.clock.advance(LedgerPolicy().claim_seconds + 1)
            attached = world.attachment("run-next").attach(
                world.cluster, world.request("run-next", artifacts), labels=LedgerLabels()
            )
            operation = ledger_operation_id(key, 1)

        assert isinstance(attached, Attached)
        assert attached.operation_id == operation
        assert attached.handle.stages[0].name == "benchmark"
        _assert_lands_in(attached, world.request("run-next", artifacts))
        assert world.state(key) is LedgerState.SUBMITTED
        observed = world.cluster.inspect(operation)
        assert isinstance(observed, ClusterObservation)
        assert observed.job_id == attached.handle.job.job_id
        # The asker's job is the dead submitter's job, if it had one: one job in all.
        again = world.attachment("run-third").attach(
            world.cluster, world.request("run-third", artifacts), labels=LedgerLabels()
        )
        assert isinstance(again, Attached)
        assert again.handle.job.job_id == attached.handle.job.job_id
        _assert_lands_in(again, world.request("run-third", artifacts))


def _assert_lands_in(attached: Attached, request: SlurmBatchRequest) -> None:
    """Every artifact lands where this asker's request declares, inside its own workspace."""
    declared = {
        (item.remote_path, item.local_path)
        for stage in request.stages
        for item in (*stage.file_artifacts, *stage.tree_artifacts)
    }
    landed = {
        (item.remote_path, item.local_path)
        for stage in attached.handle.stages
        for item in (*stage.file_artifacts, *stage.tree_artifacts)
    }
    assert landed == declared
    assert all(local.is_relative_to(request.workspace) for _, local in landed)


def test_a_rejected_submission_fails_its_attempt_and_the_next_ask_claims_a_new_one(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path)
    run = world.attachment("run-a")
    key = run.key(world.request("run-a"))
    world.cluster.script(ledger_operation_id(key, 0), rejected_reason="invalid partition")

    with pytest.raises(SlurmSubmissionRejectedError):
        run.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())

    assert world.state(key) is LedgerState.FAILED
    retried = run.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())
    assert isinstance(retried, Attached)
    assert retried.operation_id == ledger_operation_id(key, 1)


def test_an_attached_run_leases_its_request(tmp_path: Path) -> None:
    world = _World(tmp_path)
    run = world.attachment("run-a")
    attached = run.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())
    assert isinstance(attached, Attached)
    entry = world.ledger.read(attached.key)
    assert entry is not None
    assert [lease.holder for lease in entry.leases] == ["run-a"]
    assert entry.leases[0].expires_at == world.clock.now() + LedgerPolicy().lease_seconds

    run.release(attached.key)
    entry = world.ledger.read(attached.key)
    assert entry is not None
    assert entry.leases == ()


@pytest.mark.parametrize(
    "ended",
    [SlurmJobStatus.FAILED, SlurmJobStatus.CANCELLED],
    ids=["node_fail_or_timeout", "preempted_or_scancelled"],
)
def test_a_job_that_ended_unsuccessfully_is_run_again_by_the_next_asker(
    tmp_path: Path, ended: SlurmJobStatus
) -> None:
    """NODE_FAIL, preemption, a timeout or an admin scancel never leaves a key unusable."""
    world = _World(tmp_path)
    first = world.attachment("run-a")
    key = first.key(world.request("run-a"))
    world.cluster.script(ledger_operation_id(key, 0), states=(ended,))
    assert isinstance(
        first.attach(world.cluster, world.request("run-a"), labels=LedgerLabels()), Attached
    )

    again = world.attachment("run-b").attach(
        world.cluster, world.request("run-b"), labels=LedgerLabels()
    )

    assert isinstance(again, Attached)
    assert again.operation_id == ledger_operation_id(key, 1)
    entry = world.ledger.read(key)
    assert entry is not None
    assert (entry.record.attempt, entry.record.state) == (1, LedgerState.SUBMITTED)


def _batch_result(stage_exit: int | None) -> SlurmBatchResult:
    return SlurmBatchResult(
        job_id="0",
        job_exit_code=0,
        job_output="",
        stages=(
            SlurmBatchStageResult(
                name="benchmark",
                exit_code=stage_exit,
                stdout="",
                stderr="",
                elapsed_seconds=1.0,
                skipped=False,
            ),
        ),
        phase_timings_seconds={},
        content_cache_hits=0,
    )


@pytest.mark.parametrize(
    ("stage_exit", "reused"),
    [(0, True), (1, True), (124, False), (137, False), (143, False), (None, False)],
    ids=["passed", "failed_stage", "timed_out", "killed", "terminated", "uncollectable"],
)
def test_a_completed_job_is_reused_only_once_collected_with_its_stages_own_results(
    tmp_path: Path, stage_exit: int | None, *, reused: bool
) -> None:
    """Slurm says COMPLETED whatever the stages did: the batch script always exits 0.

    A stage that ran to an exit code, a failing one too, is an evaluation result
    every later asker reuses. A stage its time limit or a signal stopped, or one
    that left no result (collection reports it Unknown), is run again.
    """
    world = _World(tmp_path)
    first = world.attachment("run-a")
    key = first.key(world.request("run-a"))
    operation = ledger_operation_id(key, 0)
    world.cluster.script(
        operation, states=(SlurmJobStatus.COMPLETED,), result=_batch_result(stage_exit)
    )
    attached = first.attach(world.cluster, world.request("run-a"), labels=LedgerLabels())
    assert isinstance(attached, Attached)

    # Before any run collected it, a later asker attaches to the ended job to collect it.
    early = world.attachment("run-b").attach(
        world.cluster, world.request("run-b"), labels=LedgerLabels()
    )
    assert isinstance(early, Attached)
    assert early.operation_id == operation
    assert world.state(key) is LedgerState.SUBMITTED

    collected = world.cluster.collect(attached.handle)
    first.settle(key, operation, SlurmJobStatus.COMPLETED, collected)

    later = world.attachment("run-c").attach(
        world.cluster, world.request("run-c"), labels=LedgerLabels()
    )
    assert isinstance(later, Attached)
    if reused:
        assert world.state(key) is LedgerState.COMPLETED
        assert later.handle.job.job_id == attached.handle.job.job_id
    else:
        assert later.operation_id == ledger_operation_id(key, 1)
        entry = world.ledger.read(key)
        assert entry is not None
        assert (entry.record.attempt, entry.record.state) == (1, LedgerState.SUBMITTED)


def test_an_edited_setup_script_is_another_request(tmp_path: Path) -> None:
    """The job sources the setup script at run time, so the key follows its content."""
    world = _World(tmp_path)
    run = world.attachment("run-a")
    request = dataclasses.replace(world.request("run-a"), setup_script="/site/setup.sh")
    world.ledger.put_operator_file("/site/setup.sh", b"module load cuda/12\n")
    before = run.key(request)
    world.ledger.put_operator_file("/site/setup.sh", b"module load cuda/13\n")
    assert run.key(request) != before


@given(remove_at=st.sampled_from(("before_claim", "before_sbatch", "before_record", "after")))
@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
def test_an_operator_removal_racing_a_submitter_is_never_overwritten(remove_at: str) -> None:
    """``evals remove`` at any point of a submission: no job is left running under a withdrawal.

    The removal runs between the submitter's steps (from the cluster's own
    dispatch and acceptance points); the submitter's record write is a
    compare-and-swap, so it loses to the withdrawal instead of erasing it.
    """
    with TemporaryDirectory() as directory:
        world = _World(Path(directory))
        submitter = world.attachment("run-a")
        request = world.request("run-a")
        key = submitter.key(request)
        operation = ledger_operation_id(key, 0)

        def remove() -> None:
            withdraw(
                world.ledger,
                world.cluster,
                key,
                Withdrawer(
                    by="vibesys-evals",
                    reason="removed by operator",
                    timing=WithdrawalTiming(
                        clock=world.clock, call_seconds=CONFIG.transport_timeout_seconds
                    ),
                ),
            )

        if remove_at == "before_sbatch":
            world.cluster.script(operation, queue_wait_s=math.inf, on_dispatch=remove)
        else:
            world.cluster.script(operation, queue_wait_s=math.inf)
        if remove_at == "before_record":
            world.cluster.on_accept(operation, remove)
        if remove_at == "before_claim":
            remove()

        try:
            outcome: object = submitter.attach(world.cluster, request, labels=LedgerLabels())
        except LedgerWithdrawnError as error:
            outcome = error
        if remove_at == "after":
            remove()

        entry = world.ledger.read(key)
        assert entry is not None
        if remove_at == "before_claim":
            assert isinstance(outcome, Attached)  # nothing to remove yet: it ran normally
            assert entry.record.state is LedgerState.SUBMITTED
            return
        assert entry.record.state is LedgerState.WITHDRAWN
        assert entry.record.reason == "removed by operator"
        observed = world.cluster.inspect(operation)
        assert not isinstance(observed, ClusterObservation) or not observed.status.active


def _active(world: _World, operation: str) -> bool:
    observed = world.cluster.inspect(operation)
    return isinstance(observed, ClusterObservation) and observed.status.active


_STAGING = st.lists(
    st.tuples(st.floats(min_value=0, max_value=1000, allow_nan=False), st.booleans()),
    min_size=1,
    max_size=5,
)


@given(staging=_STAGING)
@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
def test_a_waiter_takes_a_claim_over_only_once_it_expired_on_its_latest_renewal(
    staging: list[tuple[float, bool]],
) -> None:
    """A slow submitter stages its job while a waiter asks, again and again.

    Staging passes in steps; after each, the submitter renews its claim or does
    not (as one that hangs or died), and the waiter asks. The waiter takes over
    exactly when the claim has run out on its latest renewal; a submitter that
    keeps renewing keeps its claim and its job. Either way one job runs.
    """
    with TemporaryDirectory() as directory:
        world = _World(Path(directory))
        submitter, waiter = world.attachment("run-a"), world.attachment("run-b")
        request = world.request("run-a")
        key = submitter.key(request)
        first, second = ledger_operation_id(key, 0), ledger_operation_id(key, 1)
        window = LedgerPolicy().claim_seconds
        renewed = world.clock.now()  # the claim is its first renewal
        took_over = False

        def stage_slowly() -> None:
            nonlocal renewed, took_over
            for gap, renews in staging:
                world.clock.advance(gap)
                if renews:
                    submitter.renew(key)
                    renewed = world.clock.now()
                expired = world.clock.now() > renewed + window
                asked = waiter.attach(world.cluster, world.request("run-b"), labels=LedgerLabels())
                if took_over:
                    continue
                if expired:
                    took_over = True
                    assert isinstance(asked, Attached)
                    assert asked.operation_id == second
                else:
                    assert isinstance(asked, AttachPending)

        world.cluster.script(first, queue_wait_s=math.inf, on_dispatch=stage_slowly)
        world.cluster.script(second, queue_wait_s=math.inf)
        outcome = submitter.attach(world.cluster, request, labels=LedgerLabels())

        entry = world.ledger.read(key)
        assert entry is not None
        assert entry.record.state is LedgerState.SUBMITTED
        assert isinstance(outcome, Attached)
        kept = second if took_over else first
        assert (entry.record.operation_id, outcome.operation_id) == (kept, kept)
        assert _active(world, kept)
        assert not _active(world, first if took_over else second)


class _InterleavingCluster(FakeCluster):
    """A cluster that runs one step of another process right after its next inspection."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock=clock)
        self.after_inspect: list[Callable[[], None]] = []

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        observed = super().inspect(target, by_job_id=by_job_id)
        if self.after_inspect:
            self.after_inspect.pop()()
        return observed


def test_a_waiter_never_cancels_a_job_its_submitter_recorded_during_the_takeover(
    tmp_path: Path,
) -> None:
    """The waiter finds no job under the expired claim; the submitter then sbatches and
    records it before the waiter's swap. The swap loses, and the job runs on."""
    world = _World(tmp_path)
    world.cluster = _InterleavingCluster(world.clock)
    request = world.request("run-a")
    key = world.attachment("run-a").key(request)
    operation = ledger_operation_id(key, 0)
    record = world.claim("run-a", key)
    world.cluster.script(operation, queue_wait_s=math.inf)
    world.clock.advance(LedgerPolicy().claim_seconds + 1)

    def submitter_records_its_job() -> None:
        submitted = world.cluster.submit(request, operation_id=operation)
        assert isinstance(submitted, ClusterSubmitted)
        assert isinstance(submitted.handle, SlurmBatchHandle)
        entry = world.ledger.read(key)
        assert entry is not None
        job = job_of_handle(submitted.handle)
        assert world.ledger.replace(entry, transitioned(record, LedgerState.SUBMITTED, job=job))

    world.cluster.after_inspect.append(submitter_records_its_job)
    asked = world.attachment("run-b").attach(
        world.cluster, world.request("run-b"), labels=LedgerLabels()
    )

    assert isinstance(asked, Attached)
    assert asked.operation_id == operation
    assert world.state(key) is LedgerState.SUBMITTED
    assert _active(world, operation)


@dataclass
class _Transport:
    """The Fake cluster's transport: every inspect and cancel call fails while ``failing``."""

    failing: bool = False

    def __call__(self, _call: ClusterCall) -> None:
        if self.failing:
            raise SlurmError.transport_timed_out("exec")


@pytest.mark.parametrize(
    "submitter",
    [pytest.param("run-a", id="another-run"), pytest.param("run-b", id="this-run-earlier-process")],
)
def test_a_failed_inspect_of_a_lapsed_claim_never_starts_a_second_live_job(
    tmp_path: Path, submitter: str
) -> None:
    """A dead submitter's job runs, unrecorded; the next asker's inspect and cancel both fail.

    The asker must leave the attempt as it was, rather than end it and start the
    next attempt's job beside the first: a transport failure is not "no job".
    """
    world = _World(tmp_path)
    transport = _Transport()
    world.cluster = FakeCluster(clock=world.clock, on_call=transport)
    request = world.request(submitter)
    key = world.attachment(submitter).key(request)
    first = ledger_operation_id(key, 0)
    world.claim(submitter, key)
    world.cluster.script(first, queue_wait_s=math.inf)
    assert isinstance(world.cluster.submit(request, operation_id=first), ClusterSubmitted)
    world.clock.advance(LedgerPolicy().claim_seconds + 1)
    second = ledger_operation_id(key, 1)
    world.cluster.script(second, queue_wait_s=math.inf)

    transport.failing = True
    asked = world.attachment("run-b").attach(
        world.cluster, world.request("run-b"), labels=LedgerLabels()
    )
    transport.failing = False

    live = [operation for operation in (first, second) if _active(world, operation)]
    assert len(live) <= 1, f"live jobs of attempts {live}"
    assert isinstance(asked, AttachPending)
    assert world.state(key) is LedgerState.SUBMITTING


def test_a_claim_whose_owner_died_between_its_cancel_and_its_swap_is_taken_over(
    tmp_path: Path,
) -> None:
    """r3223 LOW-2: run-a cancelled its own lapsed claim's name, then died before its swap.

    Its lost submission never published an intent, so only the cancel's tombstone is
    left, and no job can start under the name. Another run must take the claim over;
    reading the tombstone as "a job may run" left the request pending forever.
    """
    world = _World(tmp_path)
    key = world.attachment("run-a").key(world.request("run-a"))
    first, second = ledger_operation_id(key, 0), ledger_operation_id(key, 1)
    world.claim("run-a", key)
    # ``_take_over_own``'s first step; the swap that should follow never runs.
    assert isinstance(world.cluster.cancel(first), ClusterCancelRequested)
    world.clock.advance(LedgerPolicy().claim_seconds + 1)
    world.cluster.script(second, queue_wait_s=math.inf)

    asked = world.attachment("run-b").attach(
        world.cluster, world.request("run-b"), labels=LedgerLabels()
    )

    assert isinstance(asked, Attached)
    assert asked.operation_id == second
    assert _active(world, second)
