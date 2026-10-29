"""The per-run cap on concurrently submitted agent GPU jobs.

Every job is a simulated thread on the virtual clock, and a holding launcher
stands in for ``srun``: it counts the jobs submitted to it and holds each one
for its scripted time or until it is cancelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    CANCELLED_BEFORE_SUBMISSION,
    CappedGpuLauncher,
    GpuCommand,
    GpuJobOutcome,
    GpuJobRequest,
)
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Event, Threads, Worker

_REQUEST = GpuJobRequest(gpus=4, time_minutes=30)
_EXIT = 7
_EARLY_REPLACEMENT = "a replacement was submitted while the prior job remained alive"


class _HoldingLauncher:
    """Fake ``srun``: records each submitted job and holds it for its scripted seconds."""

    def __init__(self, threads: Threads, holds: dict[str, float]) -> None:
        self.submitted: list[str] = []
        self.active = 0
        self.most_active = 0
        self._holds = holds
        self._lock = threads.lock()

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        del request
        (name,) = command.argv
        with self._lock:
            self.submitted.append(name)
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        write(b"[srun] started\n")
        try:
            cancel.wait(self._holds[name])
        finally:
            with self._lock:
                self.active -= 1
        return GpuJobOutcome(_EXIT)


def _command(name: str, *, job_name: str | None = None) -> GpuCommand:
    return GpuCommand(argv=(name,), cwd=Path("workspace"), env={}, job_name=job_name)


class _UnconfirmedThenSettledLauncher:
    """Keep the first scheduler job alive until its stable identity is reconciled."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.reconciled: list[str] = []
        self.remote_active = False

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        del request, write, cancel
        (name,) = command.argv
        if self.remote_active:
            raise AssertionError(_EARLY_REPLACEMENT)
        self.submitted.append(name)
        if len(self.submitted) == 1:
            self.remote_active = True
            return GpuJobOutcome(
                255,
                termination_confirmed=False,
                job_name=command.job_name,
            )
        return GpuJobOutcome(0, job_name=command.job_name)

    def reconcile(self, job_name: str) -> bool:
        self.reconciled.append(job_name)
        self.remote_active = False
        return True


@dataclass(frozen=True)
class _Job:
    arrives: int
    """Virtual second the agent runs its command; distinct per job, so arrival order is fixed."""
    holds: float
    """Seconds the job runs once submitted, unless cancelled first."""
    cancelled_at: float | None
    """Virtual second the agent kills its command, if it does."""


@dataclass
class _Outcome:
    status: int | None = None
    output: bytes = b""


_jobs = st.lists(
    st.builds(
        _Job,
        arrives=st.integers(0, 30),
        holds=st.integers(1, 15),
        cancelled_at=st.none() | st.integers(0, 60),
    ),
    min_size=1,
    max_size=8,
    unique_by=lambda job: job.arrives,
)


@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
# Two requests wait when the slot frees: the later one must not overtake the earlier one.
@example(
    cap=1,
    jobs=[_Job(0, 2, None), _Job(1, 1, None), _Job(2, 1, None)],
    seed=None,
)
@given(
    cap=st.integers(1, 4),
    jobs=_jobs,
    seed=st.none() | st.integers(0, 2**32),
)
def test_at_most_cap_jobs_are_ever_submitted_and_every_request_is_submitted_or_cancelled(
    cap: int, jobs: list[_Job], seed: int | None
) -> None:
    threads = SimThreads(schedule_seed=seed)
    names = [f"job-{index}" for index in range(len(jobs))]
    inner = _HoldingLauncher(
        threads, {name: job.holds for name, job in zip(names, jobs, strict=True)}
    )
    launcher = CappedGpuLauncher(inner, cap, threads)
    outcomes = {name: _Outcome() for name in names}

    def agent(name: str, job: _Job, cancel: Event) -> None:
        threads.sleep(job.arrives)
        outcome = outcomes[name]

        def write(chunk: bytes) -> None:
            outcome.output += chunk

        outcome.status = launcher.run(
            _REQUEST, _command(name), write=write, cancel=cancel
        ).exit_code

    def kill(cancel: Event, at: float) -> None:
        threads.sleep(at)
        cancel.set()

    # Every job is over once all have arrived, run back to back and been cancelled; the
    # hang guard on top only catches a job that never ends.
    horizon = HANG_GUARD_S + sum(job.arrives + job.holds + (job.cancelled_at or 0) for job in jobs)

    def program() -> None:
        workers: list[Worker] = []
        for name, job in zip(names, jobs, strict=True):
            cancel = threads.event()
            workers.append(threads.spawn(lambda n=name, j=job, c=cancel: agent(n, j, c), name=name))
            if job.cancelled_at is not None:
                at = job.cancelled_at
                workers.append(threads.spawn(lambda c=cancel, a=at: kill(c, a), name="kill"))
        for worker in workers:
            worker.join(horizon)
            assert not worker.is_alive(), f"{worker.name} never finished"

    threads.run(program)

    assert inner.most_active <= cap
    assert inner.active == 0
    submitted = set(inner.submitted)
    assert len(submitted) == len(inner.submitted), "a request was submitted twice"
    for name, job in zip(names, jobs, strict=True):
        outcome = outcomes[name]
        if name in submitted:
            assert outcome.status == _EXIT
        else:
            # Never submitted: only because the agent gave up while it waited.
            assert job.cancelled_at is not None
            assert outcome.status == CANCELLED_BEFORE_SUBMISSION
            assert b"[srun]" not in outcome.output
    # First come, first served: submissions keep arrival order.
    arrival = {name: job.arrives for name, job in zip(names, jobs, strict=True)}
    assert inner.submitted == sorted(inner.submitted, key=arrival.__getitem__)


def test_a_request_cancelled_while_it_waits_is_never_submitted_and_frees_its_place() -> None:
    threads = SimThreads()
    # The first job would outlive every hang guard: only its cancel ends it.
    inner = _HoldingLauncher(threads, {"first": 10 * HANG_GUARD_S, "second": 1, "third": 1})
    launcher = CappedGpuLauncher(inner, 1, threads)
    first_cancel, second_cancel = threads.event(), threads.event()
    second_waiting = threads.event()
    second_output = bytearray()
    statuses: dict[str, int] = {}

    def second_write(chunk: bytes) -> None:
        second_output.extend(chunk)
        second_waiting.set()

    def program() -> None:
        first = threads.spawn(
            lambda: statuses.__setitem__(
                "first",
                launcher.run(
                    _REQUEST, _command("first"), write=lambda _c: None, cancel=first_cancel
                ).exit_code,
            ),
            name="first",
        )
        threads.sleep(1)
        second = threads.spawn(
            lambda: statuses.__setitem__(
                "second",
                launcher.run(
                    _REQUEST, _command("second"), write=second_write, cancel=second_cancel
                ).exit_code,
            ),
            name="second",
        )
        assert second_waiting.wait(HANG_GUARD_S), "the second request never reported waiting"
        second_cancel.set()  # the agent kills its command while it waits
        second.join(HANG_GUARD_S)
        assert not second.is_alive(), "a cancelled request waited for a slot instead of leaving"
        assert inner.submitted == ["first"]
        first_cancel.set()  # now the slot frees
        first.join(HANG_GUARD_S)
        assert not first.is_alive()
        # The cancelled request left no claim behind: the next one is submitted at once.
        statuses["third"] = launcher.run(
            _REQUEST, _command("third"), write=lambda _c: None, cancel=threads.event()
        ).exit_code

    threads.run(program)

    assert inner.submitted == ["first", "third"]
    assert statuses == {"first": _EXIT, "second": CANCELLED_BEFORE_SUBMISSION, "third": _EXIT}
    assert second_output.decode() == (
        "[vibesys-gpu] waiting: 1 of this run's GPU jobs are active (cap 1), 0 queued ahead; "
        "the job is submitted, and its --time starts, when a slot frees\n"
    )


def test_an_unconfirmed_job_keeps_its_slot_until_identity_reconciliation() -> None:
    """At cap one, a lost wrapper cannot admit a replacement stable-name job."""
    threads = SimThreads()
    inner = _UnconfirmedThenSettledLauncher()
    launcher = CappedGpuLauncher(inner, 1, threads)
    waiting = threads.event()
    outcomes: list[GpuJobOutcome] = []
    job_name = "vibesys-stable-capture"

    def replacement() -> None:
        outcomes.append(
            launcher.run(
                _REQUEST,
                _command("replacement", job_name=job_name),
                write=lambda _chunk: waiting.set(),
                cancel=threads.event(),
            )
        )

    def program() -> None:
        first = launcher.run(
            _REQUEST,
            _command("first", job_name=job_name),
            write=lambda _chunk: None,
            cancel=threads.event(),
        )
        assert not first.termination_confirmed
        assert first.job_name == job_name
        worker = threads.spawn(replacement, name="replacement")
        assert waiting.wait(HANG_GUARD_S), "the replacement did not wait for reconciliation"
        assert inner.submitted == ["first"]
        assert launcher.reconcile(job_name)
        worker.join(HANG_GUARD_S)
        assert not worker.is_alive(), "the reconciled slot did not admit its waiter"

    threads.run(program)

    assert inner.reconciled == [job_name]
    assert inner.submitted == ["first", "replacement"]
    assert len(outcomes) == 1
    assert outcomes[0].termination_confirmed
