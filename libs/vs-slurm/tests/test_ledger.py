"""The ledger's pure parts (key, attach decision, lease verdict) and its withdrawal over Fakes."""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    AttachJob,
    CheckJob,
    ClaimAttempt,
    ClusterObservation,
    ClusterSubmitted,
    FakeCluster,
    FakeEvaluationLedger,
    Leased,
    LedgerEntry,
    LedgerError,
    LedgerLease,
    LedgerRecord,
    LedgerStage,
    LedgerState,
    NotInLedger,
    ResolveSubmission,
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmJobStatus,
    SlurmSshTransport,
    SlurmTreeArtifact,
    SubmitBoundJob,
    Unleased,
    Withdrawn,
    evaluation_key,
    handle_of_job,
    job_of_handle,
    lease_verdict,
    ledger_operation_id,
    list_entries,
    settled_state,
    submit_step,
    sweep_unleased,
    withdraw,
)

PROJECT = "demo-0123456789ab"
CONFIG = SlurmConfig(
    name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="h")
)


def _request(workspace: Path, command: str = "true") -> SlurmBatchRequest:
    return SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(
                name="profile",
                command=("bash", "-c", command),
                tree_artifacts=(
                    SlurmTreeArtifact(remote_path="traces", local_path=workspace / "traces"),
                ),
            ),
        ),
    )


def _key(project: str, request: SlurmBatchRequest, config: SlurmConfig) -> str:
    return evaluation_key(project, request, config, setup_script_sha256=None)


def _write_tree(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")


_FILES = st.dictionaries(
    st.sampled_from(("a.py", "b.txt", "c.cfg")), st.text(max_size=8), min_size=1, max_size=3
)


@given(files=_FILES, command=st.sampled_from(("true", "python bench.py")))
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_key_follows_content_and_spec_and_never_the_workspace_path(
    files: dict[str, str], command: str
) -> None:
    """A fresh relaunch evaluates the same content from a new worktree path: the same key."""
    with TemporaryDirectory() as directory:
        first, second = Path(directory) / "run-1" / "wt", Path(directory) / "run-2" / "other"
        _write_tree(first, files)
        _write_tree(second, files)
        key = _key(PROJECT, _request(first, command), CONFIG)
        assert key == _key(PROJECT, _request(second, command), CONFIG)
        assert key != _key("other-project", _request(first, command), CONFIG)
        assert key != _key(PROJECT, _request(first, command + " --x"), CONFIG)
        (second / "a.py").write_text("changed content", encoding="utf-8")
        if files.get("a.py") != "changed content":
            assert key != _key(PROJECT, _request(second, command), CONFIG)
        resized = CONFIG.model_copy(update={"sbatch_arguments": ("--gpus=2",)})
        assert key != _key(PROJECT, _request(first, command), resized)


@given(
    files=_FILES,
    modes=st.lists(st.sampled_from((0o600, 0o644, 0o664, 0o640)), min_size=2, max_size=2),
    executable=st.booleans(),
)
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_key_sees_the_executable_bit_and_no_other_mode_bit(
    files: dict[str, str], modes: list[int], *, executable: bool
) -> None:
    """A checkout under another umask asks for the same key; making a file executable does not."""
    with TemporaryDirectory() as directory:
        first, second = Path(directory) / "a", Path(directory) / "b"
        _write_tree(first, files)
        _write_tree(second, files)
        name = sorted(files)[0]
        exec_bits = 0o111 if executable else 0
        (first / name).chmod(modes[0] | exec_bits)
        (second / name).chmod(modes[1] | exec_bits)
        key = _key(PROJECT, _request(first), CONFIG)
        assert key == _key(PROJECT, _request(second), CONFIG)
        (second / name).chmod(modes[1] | (0 if executable else 0o100))
        assert key != _key(PROJECT, _request(second), CONFIG)


@given(files=_FILES, name=st.sampled_from(("pipe", "sub/pipe", "z.fifo")))
@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
def test_the_key_skips_a_fifo_without_opening_it(files: dict[str, str], name: str) -> None:
    """Opening a FIFO with no writer blocks forever; the key never opens one.

    A regression hangs here instead of returning.
    """
    with TemporaryDirectory() as directory:
        plain, piped = Path(directory) / "plain", Path(directory) / "piped"
        _write_tree(plain, files)
        _write_tree(piped, files)
        (plain / name).parent.mkdir(parents=True, exist_ok=True)
        (piped / name).parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(piped / name)
        assert _key(PROJECT, _request(piped), CONFIG) == _key(PROJECT, _request(plain), CONFIG)


@given(
    script=st.sampled_from(("/opt/site/setup.sh", "/home/op/env.sh")),
    contents=st.lists(st.binary(max_size=16), min_size=2, max_size=2),
)
@settings(max_examples=examples(pr=10, nightly=100), deadline=None)
def test_the_key_follows_the_setup_script_content_the_job_sources(
    script: str, contents: list[bytes]
) -> None:
    """The job sources the setup script at run time, so an edited script is another request."""
    with TemporaryDirectory() as directory:
        workspace = Path(directory) / "wt"
        _write_tree(workspace, {"a.py": "x"})
        request = dataclasses.replace(_request(workspace), setup_script=script)
        digests = [hashlib.sha256(content).hexdigest() for content in contents]
        keys = [
            evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=digest)
            for digest in (*digests, None)
        ]
        assert (keys[0] == keys[1]) == (contents[0] == contents[1])
        assert keys[2] not in keys[:2]  # a missing script is not any script
        assert _key(PROJECT, _request(workspace), CONFIG) not in keys


_JOB = SubmitBoundJob(
    job_id="7",
    config_identity="0" * 64,
    remote_workspace="/r/op/workspace",
    remote_status_path="/r/op/exit-code.txt",
    remote_log_path="/r/op/job.log",
    stop_on_failure=True,
    stages=(LedgerStage(name="s"),),
)


def _record(
    state: LedgerState,
    *,
    attempt: int = 0,
    created_at: float = 0.0,
    claim_expires_at: float = 900.0,
    key: str = "c" * 64,
) -> LedgerRecord:
    return LedgerRecord(
        key=key,
        project=PROJECT,
        attempt=attempt,
        operation_id=ledger_operation_id(key, attempt),
        owner="run-a",
        created_at=created_at,
        claim_expires_at=claim_expires_at,
        state=state,
        job=_JOB if state in {LedgerState.SUBMITTED, LedgerState.COMPLETED} else None,
        reason="gone" if state in {LedgerState.FAILED, LedgerState.WITHDRAWN} else None,
    )


def _handle(workspace: Path) -> SlurmBatchHandle:
    cluster = FakeCluster(clock=ManualClock())
    workspace.mkdir(parents=True, exist_ok=True)
    request = SlurmBatchRequest(
        workspace=workspace,
        stages=(
            SlurmBatchStage(
                name="profile",
                command=("true",),
                tree_artifacts=(
                    SlurmTreeArtifact(remote_path="traces", local_path=workspace / "traces"),
                ),
            ),
        ),
    )
    submitted = cluster.submit(request, operation_id="ev-op")
    assert isinstance(submitted, ClusterSubmitted)
    assert isinstance(submitted.handle, SlurmBatchHandle)
    return submitted.handle


@given(
    state=st.sampled_from(LedgerState),
    attempt=st.integers(min_value=0, max_value=5),
    now=st.floats(min_value=0, max_value=2000),
)
@settings(max_examples=examples(pr=30, nightly=200), deadline=None)
def test_only_an_absent_failed_or_withdrawn_request_is_submitted_again(
    state: LedgerState, attempt: int, now: float
) -> None:
    entry = LedgerEntry(_record(state, attempt=attempt, claim_expires_at=900.0))
    step = submit_step(entry, now=now)
    match state:
        case LedgerState.COMPLETED:
            assert step == AttachJob(entry.record)
        case LedgerState.SUBMITTED:
            assert step == CheckJob(entry)
        case LedgerState.SUBMITTING:
            assert step == ResolveSubmission(entry, expired=now > 900.0)
        case LedgerState.FAILED | LedgerState.WITHDRAWN:
            assert step == ClaimAttempt(attempt + 1)
    assert submit_step(None, now=now) == ClaimAttempt(0)


@given(status=st.sampled_from(SlurmJobStatus))
def test_only_a_job_that_finished_successfully_settles_as_reusable(status: SlurmJobStatus) -> None:
    settled = settled_state(status)
    if not status.terminal:
        assert settled is None
    elif status is SlurmJobStatus.COMPLETED:
        assert settled is LedgerState.COMPLETED
    else:
        assert settled is LedgerState.FAILED
        # A failed attempt is final: the next ask claims a new one.
        assert submit_step(LedgerEntry(_record(settled)), now=0.0) == ClaimAttempt(1)


_TIMES = st.floats(min_value=0, max_value=1e6, allow_nan=False)


@given(
    created=_TIMES,
    expiries=st.lists(_TIMES, max_size=3),
    grace=st.floats(min_value=0, max_value=1e4),
    now=_TIMES,
)
@settings(max_examples=examples(pr=50, nightly=500), deadline=None)
def test_a_request_is_unleased_only_after_its_newest_lease_and_the_grace_period(
    created: float, expiries: list[float], grace: float, now: float
) -> None:
    leases = tuple(
        LedgerLease(holder=f"run-{index}", renewed_at=0.0, expires_at=expiry)
        for index, expiry in enumerate(expiries)
    )
    entry = LedgerEntry(_record(LedgerState.SUBMITTED, created_at=created), leases)
    newest = max(expiries, default=created)
    verdict = lease_verdict(entry, now=now, grace_seconds=grace)
    if now <= newest + grace:
        assert verdict == Leased(newest + grace)
    else:
        assert verdict == Unleased(newest)
    # Monotone: once unleased, a later reading never finds it leased again.
    if isinstance(verdict, Unleased):
        later = lease_verdict(entry, now=now + 1, grace_seconds=grace)
        assert isinstance(later, Unleased)
    # A lease this build cannot read holds the request whatever the others say.
    unknown = dataclasses.replace(entry, unreadable_leases=("run-new",))
    assert lease_verdict(unknown, now=now, grace_seconds=grace) == Leased(math.inf)


def test_a_locator_lands_artifacts_where_the_attaching_request_declares(tmp_path: Path) -> None:
    original = tmp_path / "run-1"
    handle = _handle(original)
    job = job_of_handle(handle)
    elsewhere = tmp_path / "run-2" / "worktree"
    elsewhere.mkdir(parents=True)
    rebased = handle_of_job(job, operation_id="ev-op", request=_request(elsewhere))
    assert rebased.job.job_id == handle.job.job_id
    assert rebased.job.remote_workspace == handle.job.remote_workspace
    assert rebased.stages[0].tree_artifacts[0].local_path == elsewhere / "traces"
    assert job_of_handle(rebased) == job


def test_a_locator_refuses_a_request_whose_stages_differ(tmp_path: Path) -> None:
    job = job_of_handle(_handle(tmp_path / "run-1"))
    other = tmp_path / "run-2"
    other.mkdir()
    request = SlurmBatchRequest(
        workspace=other, stages=(SlurmBatchStage(name="profile", command=("true",)),)
    )
    with pytest.raises(LedgerError):
        handle_of_job(job, operation_id="ev-op", request=request)


class _World:
    """A ledger and a cluster over Fakes, with one request submitted under attempt 0."""

    def __init__(self, tmp_path: Path, *, status: SlurmJobStatus) -> None:
        self.clock = ManualClock()
        self.cluster = FakeCluster(clock=self.clock)
        self.ledger = FakeEvaluationLedger(project=PROJECT)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        request = _request(workspace)
        self.key = evaluation_key(PROJECT, request, CONFIG, setup_script_sha256=None)
        operation = ledger_operation_id(self.key, 0)
        self.cluster.script(operation, states=(status,))
        submitted = self.cluster.submit(request, operation_id=operation)
        assert isinstance(submitted, ClusterSubmitted)
        assert isinstance(submitted.handle, SlurmBatchHandle)
        self.job_id = submitted.handle.job.job_id
        self.ledger.claim(
            LedgerRecord(
                key=self.key,
                project=PROJECT,
                attempt=0,
                operation_id=operation,
                owner="run-a",
                created_at=0.0,
                claim_expires_at=900.0,
                state=LedgerState.SUBMITTED,
                job=job_of_handle(submitted.handle),
            )
        )
        self.ledger.renew(self.key, LedgerLease(holder="run-a", renewed_at=0.0, expires_at=300.0))

    def state(self) -> LedgerState:
        entry = self.ledger.read(self.key)
        assert entry is not None
        return entry.record.state


@pytest.mark.parametrize("status", [SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING])
def test_withdrawing_a_live_request_cancels_its_job(tmp_path: Path, status: SlurmJobStatus) -> None:
    world = _World(tmp_path, status=status)
    outcome = withdraw(world.ledger, world.cluster, world.key, reason="removed by operator")
    assert outcome == Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id)
    assert world.state() is LedgerState.WITHDRAWN
    again = withdraw(world.ledger, world.cluster, world.key, reason="again")
    assert isinstance(again, Withdrawn)
    assert again.already


def test_withdrawing_an_unknown_request_says_so(tmp_path: Path) -> None:
    world = _World(tmp_path, status=SlurmJobStatus.PENDING)
    assert withdraw(world.ledger, world.cluster, "d" * 64, reason="x") == NotInLedger("d" * 64)


@given(
    offset=st.integers(min_value=-100, max_value=100), grace=st.sampled_from((0.0, 60.0, 1800.0))
)
@settings(max_examples=examples(pr=20, nightly=200), deadline=None)
def test_an_unleased_job_is_cancelled_only_after_its_grace_period(
    offset: int, grace: float
) -> None:
    with TemporaryDirectory() as directory:
        world = _World(Path(directory), status=SlurmJobStatus.RUNNING)
        now = 300.0 + grace + offset
        outcomes = sweep_unleased(world.ledger, world.cluster, now=now, grace_seconds=grace)
        if offset <= 0:
            assert outcomes == ()
            assert world.state() is LedgerState.SUBMITTED
        else:
            assert outcomes == (
                Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id),
            )
            assert world.state() is LedgerState.WITHDRAWN


@pytest.mark.parametrize(
    ("status", "settled"),
    [
        (SlurmJobStatus.COMPLETED, LedgerState.COMPLETED),
        (SlurmJobStatus.FAILED, LedgerState.FAILED),
        (SlurmJobStatus.CANCELLED, LedgerState.FAILED),
    ],
)
def test_the_sweep_settles_an_ended_job_and_never_asks_the_cluster_about_it_again(
    tmp_path: Path, status: SlurmJobStatus, settled: LedgerState
) -> None:
    world = _World(tmp_path, status=status)
    assert sweep_unleased(world.ledger, world.cluster, now=1e9, grace_seconds=0.0) == ()
    assert world.state() is settled
    # A cluster that has forgotten the job (accounting purged it) changes nothing:
    # a settled request is not asked about again.
    forgetful = FakeCluster(clock=ManualClock())
    assert sweep_unleased(world.ledger, forgetful, now=2e9, grace_seconds=0.0) == ()
    assert world.state() is settled


def test_a_lease_this_build_cannot_read_holds_the_request_and_blocks_no_removal(
    tmp_path: Path,
) -> None:
    """A newer build's lease is a live holder: never swept, listed, and ``evals remove`` works."""
    world = _World(tmp_path, status=SlurmJobStatus.RUNNING)
    world.ledger.store_raw_lease(world.key, "run-new", '{"format_version": 2}\n')

    assert sweep_unleased(world.ledger, world.cluster, now=1e9, grace_seconds=0.0) == ()
    assert world.state() is LedgerState.SUBMITTED
    (row,) = list_entries(world.ledger, world.cluster, project=PROJECT, now=1e9).rows
    assert row.unreadable_leases == ("run-new",)
    assert row.lease_holders == ("run-a",)

    outcome = withdraw(world.ledger, world.cluster, world.key, reason="removed by operator")
    assert outcome == Withdrawn(world.key, ledger_operation_id(world.key, 0), world.job_id)
    assert world.state() is LedgerState.WITHDRAWN


def test_the_sweep_never_touches_a_job_outside_the_ledger(tmp_path: Path) -> None:
    world = _World(tmp_path, status=SlurmJobStatus.RUNNING)
    workspace = tmp_path / "other"
    workspace.mkdir()
    world.cluster.script("foreign", states=(SlurmJobStatus.RUNNING,))
    foreign = world.cluster.submit(_request(workspace, "foreign"), operation_id="foreign")
    assert isinstance(foreign, ClusterSubmitted)
    sweep_unleased(world.ledger, world.cluster, now=1e9, grace_seconds=0.0)
    observed = world.cluster.inspect("foreign")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.RUNNING
