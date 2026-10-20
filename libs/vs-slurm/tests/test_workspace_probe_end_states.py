"""Only a verdict on the root ends a run's workspace-root proof; every other job ending retries.

Regression (sweep review F1 of 2026-10-20): since the probe runs beside the run, a probe job
that an operator's ``scancel``, a preemption or a node failure ended, or that overran its time
limit, was recorded as a lasting "infrastructure" failure. That failure stopped the whole run
and cancelled its agents' turns, though such an ending says nothing about the root.

The input space: every way the scheduler can end the probe's job, reached straight from the
queue or after the job ran. A job whose script ran to an exit is a verdict: its exit code and
node token prove the root or refute it, at once and for good. Any other ending is retried
under a new scheduler identity, and Slurm work keeps waiting; only a bounded number of such
attempts ends the proof.
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock
from vs_slurm.api import (
    FakeConnector,
    SlurmConfig,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmLocalTransport,
    SlurmWorkspaceRootProbeError,
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeReport,
    WorkspaceRootProbeRetrying,
    WorkspaceRootProof,
    probe_workspace_root,
)

# test-isolation: the Fake connector's request log is an executable test double outside the API.
from vs_slurm.fake_connector import recorded_commands

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

type _Answer = Callable[[dict[str, object]], dict[str, object]]

_RUN_ID = "run-whose-probe-job-ends-oddly"
_FIRST_JOB_ID = "5000"  # the Fake's first scheduler identity
_POLL_SECONDS = 10.0
_QUEUE_SECONDS = 1800
# Guards against a hang only; every scenario settles within the probe's own bounds.
_VIRTUAL_LIMIT_SECONDS = 200_000.0
# The documented bound: this many attempts that ended without a verdict end the proof.
_INCONCLUSIVE_ATTEMPTS = 5
_NODE_MARKER = ".vibesys-node-token"


def _operation(generation: int) -> str:
    # test-isolation: an attempt's scheduler identity, which the Fake scripts by operation id.
    key = _RUN_ID if generation == 0 else f"{_RUN_ID}\0retry-{generation}"
    return "workspace-root-probe-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def _runner(
    root: Path, connector: FakeConnector, clock: VirtualClock | ManualClock
) -> SlurmJobRunner:
    remote = root / "remote"
    remote.mkdir(exist_ok=True)
    config = SlurmConfig(
        name="odd",
        remote_workspace_root=str(remote),
        transport=SlurmLocalTransport(
            kind="local",
            shell_command=("fake-connector", "shell"),
            rsync_command=("fake-connector", "rsync"),
        ),
        poll_interval_seconds=_POLL_SECONDS,
        queue_timeout_seconds=_QUEUE_SECONDS,
    )
    return SlurmJobRunner(
        config, processes=connector, scratch_root=root / "probe-state", clock=clock
    )


@dataclass(frozen=True)
class _Ending:
    """How the scheduler ends the probe's job, as ``sacct`` reports it, and what that proves.

    ``scripted`` is the public status the Fake scripts the end as; ``raw`` (when given)
    replaces the State ``sacct`` prints for it. ``node`` changes what the job left on its node.
    ``verdict`` is the proof's lasting answer (a match for the error), None when the ending
    says nothing about the root and the proof must try again.
    """

    scripted: SlurmJobStatus
    raw: str | None = None
    node: str = "intact"  # "intact", "unstaged" (exits nonzero), "wrong", "missing"
    verdict: str | None = None
    proven: bool = False


_ENDINGS = {
    "completed-proven": _Ending(SlurmJobStatus.COMPLETED, proven=True),
    "completed-nonzero-exit": _Ending(
        SlurmJobStatus.COMPLETED, node="unstaged", verdict="exited with code"
    ),
    "completed-wrong-token": _Ending(
        SlurmJobStatus.COMPLETED, node="wrong", verdict="returned the wrong node token"
    ),
    "completed-missing-token": _Ending(
        SlurmJobStatus.COMPLETED, node="missing", verdict="returned no collectable node token"
    ),
    "failed": _Ending(SlurmJobStatus.FAILED, node="unstaged", verdict="exited with code"),
    "cancelled": _Ending(SlurmJobStatus.CANCELLED),
    "preempted": _Ending(SlurmJobStatus.FAILED, raw="PREEMPTED"),
    "node_fail": _Ending(SlurmJobStatus.FAILED, raw="NODE_FAIL"),
    "timeout": _Ending(SlurmJobStatus.FAILED, raw="TIMEOUT"),
    "out_of_memory": _Ending(SlurmJobStatus.FAILED, raw="OUT_OF_MEMORY"),
    "boot_fail": _Ending(SlurmJobStatus.FAILED, raw="BOOT_FAIL"),
    "deadline": _Ending(SlurmJobStatus.FAILED, raw="DEADLINE"),
    "revoked": _Ending(SlurmJobStatus.FAILED, raw="REVOKED"),
    "special_exit": _Ending(SlurmJobStatus.FAILED, raw="SPECIAL_EXIT"),
    # A State this version cannot read: the job is never seen to end until it is cancelled.
    "unknown": _Ending(SlurmJobStatus.FAILED, raw="NOT_A_SLURM_STATE"),
    # Still running when the probe's own time limit (plus its margin) passed.
    "overran": _Ending(SlurmJobStatus.RUNNING),
}


class _Cluster:
    """Ends the first attempt's job as ``ending`` says; later attempts run normally."""

    def __init__(self, remote: Path, ending: _Ending) -> None:
        self._remote = remote
        self._ending = ending
        self._cancelled = False

    def answer(self, request: dict[str, object], answer: _Answer) -> dict[str, object]:
        command = str(request.get("command", ""))
        tokens = shlex.split(command) if request.get("operation") == "exec" else []
        first = _FIRST_JOB_ID in tokens
        if "sbatch" in tokens and self._ending.node == "unstaged" and not self._submitted():
            for workspace in self._remote.glob("*/*/workspace"):
                if workspace.is_symlink():
                    workspace.unlink()
                else:
                    shutil.rmtree(workspace)
        if "scancel" in tokens and first:
            self._cancelled = True
        response = answer(request)
        if "sbatch" in tokens and len(self._tokens()) == 1:
            self._change_node_token()
        if "sacct" in tokens and first and self._ending.raw is not None:
            stdout = str(response["stdout"])
            state, separator, rest = stdout.partition("|")
            if separator and state:
                raw = "CANCELLED" if self._cancelled else self._ending.raw
                response = {**response, "stdout": f"{raw}|{rest}"}
        return response

    def _submitted(self) -> bool:
        return bool(self._tokens())

    def _tokens(self) -> list[Path]:
        return sorted(self._remote.glob(f"*/*/workspace/{_NODE_MARKER}"))

    def _change_node_token(self) -> None:
        for token in self._tokens():
            if self._ending.node == "wrong":
                token.write_text("not-this-probes-token\n", encoding="utf-8")
            elif self._ending.node == "missing":
                token.unlink()


def _states(ending: _Ending, *, ran: bool) -> tuple[SlurmJobStatus, ...]:
    path = (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING) if ran else (SlurmJobStatus.PENDING,)
    if ending.scripted is SlurmJobStatus.RUNNING:
        return (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING)
    return (*path, ending.scripted)


def _job_names(connector: FakeConnector) -> list[str]:
    names = []
    for command in recorded_commands(connector.state):
        tokens = shlex.split(command)
        if "sbatch" in tokens:
            names.extend(token for token in tokens if token.startswith("--job-name="))
    return names


def _assert_cleaned(root: Path) -> None:
    cluster = root / "remote" / "odd"
    assert not [path for path in cluster.iterdir() if path.name != ".vibesys-content-cache"]


def _no_verdict(name: str) -> bool:
    ending = _ENDINGS[name]
    return ending.verdict is None and not ending.proven


def _cases() -> list[object]:
    cases = []
    for name, ending in _ENDINGS.items():
        for ran in (False, True):
            if ending.scripted is SlurmJobStatus.RUNNING and not ran:
                continue
            marks = (
                [pytest.mark.xfail(strict=True, reason="F1: a non-verdict ending ends the proof")]
                if _no_verdict(name)
                else []
            )
            label = f"{name}-{'after-running' if ran else 'from-the-queue'}"
            cases.append(pytest.param(name, ran, id=label, marks=marks))
    return cases


@pytest.mark.parametrize(("name", "ran"), _cases())
def test_only_a_verdict_on_the_root_settles_the_proof_and_any_other_ending_retries(
    tmp_path: Path, name: str, *, ran: bool
) -> None:
    ending = _ENDINGS[name]
    connector = FakeConnector(tmp_path / "cluster")
    connector.script(_operation(0), states=_states(ending, ran=ran), pending_reason="Priority")
    connector.intercept(_Cluster(tmp_path / "remote", ending).answer)
    threads = SimThreads(VirtualClock(limit=_VIRTUAL_LIMIT_SECONDS))
    reports: list[WorkspaceRootProbeReport] = []
    proof = WorkspaceRootProof(
        _runner(tmp_path, connector, threads.clock),
        state_root=tmp_path / "probe-state",
        run_id=_RUN_ID,
        threads=threads,
        report=reports.append,
    )
    outcome: list[bool | str] = []

    def run() -> None:
        proof.start()
        try:
            outcome.append(proof.require(threads.event()))
        except SlurmWorkspaceRootProbeError as error:
            outcome.append(str(error))
        proof.close()

    try:
        threads.run(run)
    finally:
        threads.close()

    retries = [report for report in reports if isinstance(report, WorkspaceRootProbeRetrying)]
    names = _job_names(connector)
    if ending.verdict is not None:
        # A verdict settles the proof at once and for good: no retry, no second job.
        assert isinstance(outcome[0], str)
        assert ending.verdict in outcome[0]
        assert isinstance(reports[-1], WorkspaceRootProbeFailed)
        assert not retries
        assert len(names) == 1
    elif ending.proven:
        assert outcome == [True]
        assert reports[-1] == WorkspaceRootProbeProven()
        assert len(names) == 1
    else:
        # The ending said nothing about the root: Slurm work waited through a retry, which
        # proved the root; the run was never told the proof failed.
        assert outcome == [True], outcome
        assert not [report for report in reports if isinstance(report, WorkspaceRootProbeFailed)]
        assert [retry.attempt for retry in retries] == [2]
        assert reports[-1] == WorkspaceRootProbeProven()
        assert len(names) == len(set(names)) == 2
    _assert_cleaned(tmp_path)


@pytest.mark.xfail(strict=True, reason="F1: a preempted probe ends the proof at its first attempt")
def test_attempts_without_a_verdict_are_bounded_and_the_bound_survives_a_restart(
    tmp_path: Path,
) -> None:
    """Each resume is a new process: the count of inconclusive attempts is durable.

    Every attempt's job is preempted. The first attempts fail retryably (a run would try again),
    the bound's attempt fails for good naming the attempts, and a later resume replays that
    lasting failure without submitting another job.
    """
    connector = FakeConnector(tmp_path / "cluster")
    for generation in range(_INCONCLUSIVE_ATTEMPTS + 2):
        connector.script(
            _operation(generation), states=(SlurmJobStatus.PENDING, SlurmJobStatus.FAILED)
        )

    def preempt_every_job(request: dict[str, object], answer: _Answer) -> dict[str, object]:
        response = answer(request)
        stdout = str(response.get("stdout", ""))
        if "sacct" in str(request.get("command", "")) and stdout.startswith("FAILED|"):
            response = {**response, "stdout": "PREEMPTED|" + stdout.partition("|")[2]}
        return response

    connector.intercept(preempt_every_job)
    errors: list[SlurmWorkspaceRootProbeError] = []
    for _resume in range(_INCONCLUSIVE_ATTEMPTS + 1):
        clock = ManualClock()
        with pytest.raises(SlurmWorkspaceRootProbeError) as failed:
            probe_workspace_root(
                _runner(tmp_path, connector, clock),
                state_root=tmp_path / "probe-state",
                run_id=_RUN_ID,
                pause=clock.advance,
            )
        errors.append(failed.value)

    assert [error.retryable for error in errors] == [True] * (_INCONCLUSIVE_ATTEMPTS - 1) + [
        False,
        False,
    ]
    assert all("preempted" in str(error) for error in errors)
    assert f"after {_INCONCLUSIVE_ATTEMPTS} attempts" in str(errors[-1])
    names = _job_names(connector)
    assert len(names) == len(set(names)) == _INCONCLUSIVE_ATTEMPTS
    _assert_cleaned(tmp_path)
