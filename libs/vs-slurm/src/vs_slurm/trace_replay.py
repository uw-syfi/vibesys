"""Replay recorded scheduler behavior through the same code path production uses.

A :class:`SchedulerTrace` is what a real Slurm cluster showed for one job: at
each second after an anchor event, what ``squeue`` printed and what ``sacct``
printed. :class:`TraceConnector` is a connector process (the same seam the
executable Fake uses) that answers vibesys's ``squeue``, ``sacct`` and ``scancel``
commands from such traces, so the real :class:`SlurmJobRunner` and everything
above it run over recorded scheduler behavior. Staging, job scripts and result
files are delegated to :class:`FakeConnector`; the job script runs locally and
finishes at once, and only the scheduler's *view* of the job follows the trace.

Time is a :class:`ManualClock`. It moves when the caller pauses and by a fixed
latency per command, so replays never sleep and are deterministic.

Three traces describe a job's life measured from submission; two describe how the
scheduler reacts to ``scancel`` (measured from the cancel) depending on whether the
job was queued or running when cancelled. A cancel that arrives after the job has
ended in accounting changes nothing, as on a real cluster.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .fake_connector import REQUESTS_FILE, FakeConnector

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .fake_cluster import ManualClock

_SQUEUE_FORMAT = "%T|%r|%S"
_ACCOUNTING_ACTIVE = frozenset({"PENDING", "RUNNING", "REQUEUED", "SUSPENDED"})
# Median time one remote command took in the recordings, in seconds.
DEFAULT_COMMAND_SECONDS = 1.2
_SBATCH_PREFIX = "Submitted batch job"
_INVALID_TRACE = "invalid scheduler trace"
_UNSUPPORTED_QUERY = "trace replay answers only the queries vibesys issues"


class TraceStep(BaseModel):
    """What the scheduler showed from ``at_seconds`` until the next step.

    ``queue_state`` is the ``squeue`` state, or None when the job no longer
    appears in the queue. ``accounting_state`` is the ``sacct`` state, or None
    when accounting has no row yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    at_seconds: float
    queue_state: str | None
    accounting_state: str | None
    exit_code: str | None = None
    reason: str | None = None

    @property
    def ended(self) -> bool:
        """Whether accounting reports a terminal state for the job."""
        return self.accounting_state is not None and self.accounting_state not in _ACCOUNTING_ACTIVE


class IssuedCommand(BaseModel):
    """One scheduler command vibesys sent during the recording."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at_seconds: float
    verb: Literal["sbatch", "squeue", "sacct", "scancel"]


class SchedulerTrace(BaseModel):
    """A recorded, sanitized scheduler timeline for one job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    provenance: str
    steps: tuple[TraceStep, ...]
    issued: tuple[IssuedCommand, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> SchedulerTrace:
        times = [step.at_seconds for step in self.steps]
        if not times or times[0] != 0 or times != sorted(times):
            raise ValueError(_INVALID_TRACE)
        return self

    @classmethod
    def running_forever(cls) -> SchedulerTrace:
        """A synthetic job that never ends on its own: for tests that must stop a live job."""
        return cls(
            name="running-forever",
            provenance="synthetic: a job that never ends on its own",
            steps=(
                TraceStep(
                    at_seconds=0.0, queue_state="RUNNING", accounting_state="RUNNING", reason="None"
                ),
            ),
        )

    @property
    def ends_on_its_own(self) -> bool:
        """Whether the timeline reaches a terminal accounting state with no cancel."""
        return self.steps[-1].ended

    @property
    def ended_at_seconds(self) -> float:
        """When accounting first reports a terminal state, or infinity."""
        return next((step.at_seconds for step in self.steps if step.ended), float("inf"))

    @property
    def duration_seconds(self) -> float:
        """When the last recorded change happened."""
        return self.steps[-1].at_seconds

    def at(self, elapsed: float) -> TraceStep:
        """The step in effect ``elapsed`` seconds after the anchor event."""
        current = self.steps[0]
        for step in self.steps:
            if step.at_seconds > elapsed:
                break
            current = step
        return current


class _Job:
    def __init__(self, job_id: str, name: str, submitted_at: float, base: str) -> None:
        self.job_id = job_id
        self.base = base
        self.name = name
        self.submitted_at = submitted_at
        self.cancelled_at: float | None = None
        self.reaction: SchedulerTrace | None = None


class TraceConnector:
    """A connector process whose scheduler behavior is replayed from traces.

    ``lifetime`` is the job's life from submission. ``on_cancel_pending`` and
    ``on_cancel_running`` are the scheduler's reaction to ``scancel`` for a job
    that was queued, or running, when cancelled.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-940101 [PLR0913]; each argument is an independent recorded behavior or injected effect.
        self,
        state: Path,
        *,
        clock: ManualClock,
        lifetime: SchedulerTrace,
        on_cancel_pending: SchedulerTrace,
        on_cancel_running: SchedulerTrace,
        command_seconds: float = DEFAULT_COMMAND_SECONDS,
        stage_weights: tuple[float, ...] = (),
    ) -> None:
        """Replay ``lifetime`` for every submitted job, advancing ``clock`` per command.

        ``stage_weights`` are the relative lengths of a batch's stages. The job script
        runs locally and finishes at once, so without them every stage result already
        exists; with them, stage results appear in order across the traced run.
        """
        self.clock = clock
        self.lifetime = lifetime
        self.on_cancel_pending = on_cancel_pending
        self.on_cancel_running = on_cancel_running
        self._command_seconds = command_seconds
        self._stage_weights = stage_weights
        self._inner = FakeConnector(state)
        self._jobs: dict[str, _Job] = {}

    @property
    def state(self) -> Path:
        """Directory holding the request log and the local stand-in for the remote."""
        return self._inner.state

    def commands(self) -> tuple[str, ...]:
        """Every remote command received so far, in order."""
        path = self.state / REQUESTS_FILE
        if not path.exists():
            return ()
        requests = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return tuple(str(item.get("command", item["operation"])) for item in requests)

    def submitted_at(self, job_id: str) -> float:
        """The clock reading when ``job_id`` was accepted: the lifetime trace's anchor."""
        return self._jobs[job_id].submitted_at

    def scancels(self) -> int:
        """How many ``scancel`` commands were received."""
        return sum(1 for command in self.commands() if command.startswith("scancel "))

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Answer one connector request; scheduler commands come from the traces."""
        request = json.loads(stdin) if stdin else {}
        tokens = (
            shlex.split(str(request.get("command", "")))
            if request.get("operation") == "exec"
            else []
        )
        answer = self._answer(request, tokens)
        if answer is None:
            completed = self._inner(argv, stdin=stdin, timeout=timeout)
            if "sbatch" in tokens:
                self._register(tokens, completed.stdout)
        else:
            self._log(request)
            completed = subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"version": 1, "returncode": 0, "stdout": answer, "stderr": ""}),
                "",
            )
        self.clock.advance(self._command_seconds)
        return completed

    def _answer(self, request: dict[str, object], tokens: list[str]) -> str | None:
        if tokens and tokens[0] in {"squeue", "sacct", "scancel"}:
            return self._scheduler(tokens)
        command = str(request.get("command", ""))
        if self._stage_weights and "exit-code.txt" in command and "wc -l" in command:
            return f"{self._finished_stages(command)}\n"
        return None

    def _finished_stages(self, command: str) -> int:
        """How many stage results exist, spread over the traced run in proportion to weights."""
        job = next(
            (item for item in self._jobs.values() if item.base and item.base in command),
            None,
        )
        if job is None:
            return 0
        elapsed = self.clock.now() - job.submitted_at
        start = next(
            (step.at_seconds for step in self.lifetime.steps if step.accounting_state == "RUNNING"),
            float("inf"),
        )
        end = self.lifetime.ended_at_seconds
        if elapsed < start:
            return 0
        if elapsed >= end:
            return len(self._stage_weights)
        done = (elapsed - start) / (end - start) * sum(self._stage_weights)
        finished, total = 0, 0.0
        for weight in self._stage_weights:
            total += weight
            finished += done >= total
        return finished

    def _log(self, request: dict[str, object]) -> None:
        with (self.state / REQUESTS_FILE).open("a", encoding="utf-8") as log:
            log.write(json.dumps(request) + "\n")

    def _register(self, tokens: list[str], reply: str) -> None:
        output = str(json.loads(reply)["stdout"])
        job_id = output.rsplit(maxsplit=1)[-1] if output.startswith(_SBATCH_PREFIX) else ""
        name = next(
            (token.split("=", 1)[1] for token in tokens if token.startswith("--job-name=")), ""
        )
        start = tokens.index("sbatch")
        base = tokens[tokens.index("cd") + 1] if "cd" in tokens[:start] else ""
        if job_id:
            self._jobs[job_id] = _Job(job_id, name, self.clock.now(), base)

    def _step(self, job: _Job) -> TraceStep:
        now = self.clock.now()
        if job.cancelled_at is not None and job.reaction is not None:
            return job.reaction.at(now - job.cancelled_at)
        return self.lifetime.at(now - job.submitted_at)

    def _scheduler(self, tokens: list[str]) -> str:
        if tokens[0] == "scancel":
            self._cancel(tokens[1])
            return ""
        return self._squeue(tokens) if tokens[0] == "squeue" else self._sacct(tokens)

    def _squeue(self, tokens: list[str]) -> str:
        if "-n" in tokens:
            job = self._named(tokens[tokens.index("-n") + 1])
            return f"{job.job_id}\n" if job and self._step(job).queue_state else ""
        job = self._jobs.get(tokens[tokens.index("-j") + 1])
        step = self._step(job) if job is not None else None
        if step is None or step.queue_state is None:
            return ""
        if _SQUEUE_FORMAT not in tokens:
            raise ValueError(_UNSUPPORTED_QUERY)
        return f"{step.queue_state}|{step.reason or ''}|N/A\n"

    def _sacct(self, tokens: list[str]) -> str:
        if "--name" in tokens:
            job = self._named(tokens[tokens.index("--name") + 1])
            return f"{job.job_id}\n" if job and self._step(job).accounting_state else ""
        job = self._jobs.get(tokens[tokens.index("-j") + 1])
        step = self._step(job) if job is not None else None
        if step is None or step.accounting_state is None:
            return ""
        return f"{step.accounting_state} {step.exit_code or '0:0'}\n"

    def _named(self, name: str) -> _Job | None:
        return next((job for job in self._jobs.values() if job.name == name), None)

    def _cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.cancelled_at is not None:
            return
        step = self._step(job)
        if step.ended:
            return
        pending = step.queue_state in {None, "PENDING"} and step.accounting_state in {
            None,
            "PENDING",
        }
        job.cancelled_at = self.clock.now()
        job.reaction = self.on_cancel_pending if pending else self.on_cancel_running
