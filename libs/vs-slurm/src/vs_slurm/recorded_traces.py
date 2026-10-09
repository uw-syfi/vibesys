"""Sanitized scheduler traces recorded on a production Slurm cluster.

Each trace keeps only state names, exit codes, queue reasons and seconds. No
host, user, partition, account, node, job id or path is recorded. Times are the
first poll that saw each state, so a change happened up to one poll interval
earlier. See :mod:`vs_slurm.trace_replay` for how traces are replayed.

``LIFETIMES`` are measured from submission; ``CANCEL_REACTIONS`` from the moment
the scheduler received ``scancel``.
"""

from __future__ import annotations

from .trace_replay import IssuedCommand, SchedulerTrace, TraceStep

# The recordings read ``CANCELLED+`` (sacct cuts State to 10 columns); this is the whole
# value ``sacct -P`` prints, with the canceller's uid sanitized to 0.
_CANCELLED = "CANCELLED by 0"
_RECORDING = "recorded with tiny CPU-only sleep jobs polled every 0.5 s"


def _polls(*times: float) -> tuple[IssuedCommand, ...]:
    return (
        IssuedCommand(at_seconds=0.0, verb="sbatch"),
        *(IssuedCommand(at_seconds=at, verb=verb) for at in times for verb in ("squeue", "sacct")),
    )


NORMAL_RUN = SchedulerTrace(
    name="normal-run",
    provenance=f"{_RECORDING}; a 12 s job started at once, so it was never PENDING",
    steps=(
        TraceStep(at_seconds=0.0, queue_state="RUNNING", accounting_state="RUNNING", reason="None"),
        TraceStep(
            at_seconds=12.9,
            queue_state="COMPLETING",
            accounting_state="COMPLETED",
            exit_code="0:0",
            reason="None",
        ),
        TraceStep(at_seconds=36.2, queue_state=None, accounting_state="COMPLETED", exit_code="0:0"),
    ),
    issued=_polls(0.0, 3.2, 6.4, 9.9, 12.9, 16.1, 19.6, 22.4, 25.9, 29.3, 32.7, 36.2),
)

PENDING_THEN_RUNNING = SchedulerTrace(
    name="pending-then-running",
    provenance=(
        "queue wait and job length from the evaluation smoke runs (accounting showed "
        "95 s from submission to start; the job ran 168 s, then 35 s in COMPLETING); "
        "the queue reason Priority is typical, not recorded"
    ),
    steps=(
        TraceStep(
            at_seconds=0.0, queue_state="PENDING", accounting_state="PENDING", reason="Priority"
        ),
        TraceStep(
            at_seconds=95.0, queue_state="RUNNING", accounting_state="RUNNING", reason="None"
        ),
        TraceStep(
            at_seconds=263.0,
            queue_state="COMPLETING",
            accounting_state="COMPLETED",
            exit_code="0:0",
            reason="None",
        ),
        TraceStep(
            at_seconds=298.0, queue_state=None, accounting_state="COMPLETED", exit_code="0:0"
        ),
    ),
)

JOB_ENDS_FIRST = SchedulerTrace(
    name="job-ends-first",
    provenance=(
        "evaluation smoke run whose job finished (138 s, then 41 s in COMPLETING) before "
        "the planned cancel point"
    ),
    steps=(
        TraceStep(at_seconds=0.0, queue_state="RUNNING", accounting_state="RUNNING", reason="None"),
        TraceStep(
            at_seconds=138.0,
            queue_state="COMPLETING",
            accounting_state="COMPLETED",
            exit_code="0:0",
            reason="None",
        ),
        TraceStep(
            at_seconds=179.0, queue_state=None, accounting_state="COMPLETED", exit_code="0:0"
        ),
    ),
)

CANCEL_RUNNING = SchedulerTrace(
    name="cancel-running",
    provenance=(
        f"{_RECORDING}; scancel of a RUNNING job. Accounting says CANCELLED at the first "
        "poll while the queue keeps the job in COMPLETING for 23.7 s"
    ),
    steps=(
        TraceStep(
            at_seconds=0.0,
            queue_state="COMPLETING",
            accounting_state=_CANCELLED,
            exit_code="0:0",
            reason="None",
        ),
        TraceStep(at_seconds=23.7, queue_state=None, accounting_state=_CANCELLED, exit_code="0:0"),
    ),
    issued=(IssuedCommand(at_seconds=0.0, verb="scancel"),),
)

CANCEL_RUNNING_SLOW_TEARDOWN = SchedulerTrace(
    name="cancel-running-slow-teardown",
    provenance=(
        "evaluation smoke run: the job was reported running for 30.9 s after scancel "
        "returned, twice with the same duration. Only the public status was logged; "
        "the raw state COMPLETING is taken from the cancel-running recording"
    ),
    steps=(
        TraceStep(
            at_seconds=0.0,
            queue_state="COMPLETING",
            accounting_state=_CANCELLED,
            exit_code="0:0",
            reason="None",
        ),
        TraceStep(at_seconds=30.9, queue_state=None, accounting_state=_CANCELLED, exit_code="0:0"),
    ),
    issued=(IssuedCommand(at_seconds=0.0, verb="scancel"),),
)

CANCEL_PENDING = SchedulerTrace(
    name="cancel-pending",
    provenance=f"{_RECORDING}; scancel of a job held PENDING by a begin time: gone at once",
    steps=(
        TraceStep(at_seconds=0.0, queue_state=None, accounting_state=_CANCELLED, exit_code="0:0"),
    ),
    issued=(IssuedCommand(at_seconds=0.0, verb="scancel"),),
)

LIFETIMES = {trace.name: trace for trace in (NORMAL_RUN, PENDING_THEN_RUNNING, JOB_ENDS_FIRST)}
CANCEL_REACTIONS = {
    trace.name: trace for trace in (CANCEL_RUNNING, CANCEL_RUNNING_SLOW_TEARDOWN, CANCEL_PENDING)
}
