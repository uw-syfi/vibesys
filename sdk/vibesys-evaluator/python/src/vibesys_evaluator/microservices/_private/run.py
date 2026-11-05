"""``Run``: the phase primitives ``benchmark(run)`` composes, and the orchestration behind them.

Each phase is one ``PhaseJob`` handed to the deploy layer's ``PhaseRunner``.
The orchestrator keeps the sample's write history (the writes every phase
sent, in start order) and gives it to the next phase, so state carries from
phase to phase; it judges each phase's output and raises ``BenchmarkError``
with the reason, which ends the sample with an error record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import vibesys_evaluator
from vibesys_evaluator._private.files import SchemaFileError, read_message
from vibesys_evaluator._private.records import Partial
from vibesys_evaluator._private.results import finite
from vibesys_evaluator._private.run import bind
from vibesys_evaluator._private.sample import SampleError
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private.deploy import PHASE_OUTPUT_FILE, PhaseJob
from vibesys_evaluator.microservices._private.judge import Exhausted, Failed, judge
from vibesys_evaluator.microservices._private.phase import plan_size
from vibesys_evaluator.microservices._private.results import PhaseResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys_evaluator._private.results import Validity
    from vibesys_evaluator._private.sample import Sample
    from vibesys_evaluator.microservices._private.deploy import PhaseRunner

#: Time a phase pod may spend outside loadgen's run: the model, generation, checking, copying.
PHASE_OVERHEAD_SECS = 900.0
#: A closed loop's retry plans for at least this multiple of the rate it ran out at (its whole
#: plan over the time until it stopped) ...
RETRY_RATE_MARGIN = 2.0
#: ... and at least this multiple of the bound it ran out of.
RETRY_BOUND_FACTOR = 2.0


class BenchmarkError(SampleError):
    """A phase is not a measurement; the message is the error record's."""


@dataclass(frozen=True, kw_only=True)
class Load:
    """How one phase offers load."""

    model: pb.LoadModel
    secs: float
    warmup: float
    clients: int = 0
    max_ops_per_sec: float = 0.0
    rate: float = 0.0
    arrivals: pb.Arrivals = pb.Arrivals.UNSPECIFIED

    def describe(self) -> str:
        """A short description for messages."""
        if self.model == pb.LoadModel.CLOSED:
            return f"closed loop, {self.clients} clients, {self.secs:g} s after {self.warmup:g} s warmup"
        mode = "poisson" if self.arrivals == pb.Arrivals.POISSON else "uniform"
        return f"open loop at {self.rate:.1f} ops/s ({mode}), {self.secs:g} s after {self.warmup:g} s warmup"


def _positive(name: str, value: float) -> float:
    if not finite(value) or value <= 0:
        msg = f"{name} must be a finite number > 0, got {value!r}"
        raise ValueError(msg)
    return float(value)


def _warmup(value: float) -> float:
    if not finite(value) or value < 0:
        msg = f"warmup must be a finite number >= 0, got {value!r}"
        raise ValueError(msg)
    return float(value)


def _sent_rate(load: Load, verdict: Exhausted) -> float:
    """The rate an exhausted closed loop sent at, or its window's rate if unknown.

    Its whole plan over the time from the phase's start (the window's start
    minus the warmup) until it stopped.
    """
    if verdict.stopped_unix_nanos <= 0:
        return verdict.rate
    started = verdict.window_start_unix_nanos - load.warmup * 1e9
    ran = (verdict.stopped_unix_nanos - started) / 1e9
    count = plan_size(_phase_input(load, sample_seed=0, index=0, history=[]))
    return max(verdict.rate, count / ran) if ran > 0 else verdict.rate


def _phase_input(
    load: Load, *, sample_seed: int, index: int, history: list[phase_pb.HistoryEntry]
) -> phase_pb.PhaseInput:
    return phase_pb.PhaseInput(
        sample_seed=sample_seed,
        phase_index=index,
        model=load.model,
        clients=load.clients,
        max_ops_per_sec=load.max_ops_per_sec,
        rate=load.rate,
        arrivals=load.arrivals,
        secs=load.secs,
        warmup_secs=load.warmup,
        history=history,
    )


class PhaseDriver:
    """Runs the sample's phases in order and keeps its write history."""

    def __init__(
        self,
        *,
        runner: PhaseRunner,
        sample: Sample,
        operations: Sequence[str],
        validity: Validity,
        log: Callable[[str], None],
    ) -> None:
        """Drive phases through ``runner``; their files go to the sample's phase directories."""
        self._runner = runner
        self._sample = sample
        self._seed = sample.seed
        self._operations = list(operations)
        self._validity = validity
        self._log = log
        self._history: list[phase_pb.HistoryEntry] = []
        self._next_index = 0
        self._last: PhaseResult | None = None

    @property
    def partial(self) -> Partial | None:
        """The throughput of the last phase that measured, for an error record."""
        if self._last is None:
            return None
        return Partial(
            name="last_phase_ops_per_sec",
            value=self._last.ops_per_sec,
            direction="max",
            unit="ops/s",
        )

    def phase(self, load: Load) -> PhaseResult:
        """Run one phase (a closed loop that runs out of plan is retried once with a larger one)."""
        for attempt in range(2):
            index, verdict = self._run(load)
            if isinstance(verdict, PhaseResult):
                self._log(
                    f"phase {index} ({load.describe()}): {verdict.ops_per_sec:.1f} ops/s, "
                    f"p50 {verdict.p50_ms:.2f} ms, p99 {verdict.p99_ms:.2f} ms"
                )
                self._last = verdict
                self._sample.partial = self.partial
                return verdict
            if isinstance(verdict, Failed):
                msg = f"phase {index} ({load.describe()}): {verdict.reason}"
                raise BenchmarkError(msg, partial=self.partial)
            rate = _sent_rate(load, verdict)
            bound = max(RETRY_BOUND_FACTOR * load.max_ops_per_sec, RETRY_RATE_MARGIN * rate)
            if attempt == 0:
                self._log(
                    f"phase {index} ran out of its plan of {load.max_ops_per_sec:.0f} ops/s at "
                    f"{rate:.0f} ops/s; running it again planned for {bound:.0f} ops/s"
                )
                load = replace(load, max_ops_per_sec=math.ceil(bound))
        msg = (
            f"phase {index} ({load.describe()}) ran out of its plan of {load.max_ops_per_sec:.0f} "
            f"ops/s twice: the application is faster than the plan; raise max_ops_per_sec"
        )
        raise BenchmarkError(msg, partial=self.partial)

    def _run(self, load: Load) -> tuple[int, PhaseResult | Exhausted | Failed]:
        index = self._next_index
        self._next_index += 1
        phase_input = _phase_input(
            load, sample_seed=self._seed, index=index, history=list(self._history)
        )
        name = f"phase-{index}"
        out_dir = self._sample.phase_dir(name)
        self._log(f"phase {index}: {load.describe()}")
        self._runner.run_phase(
            PhaseJob(
                name=name,
                phase_input=phase_input.to_binary(),
                timeout_secs=load.warmup + load.secs + PHASE_OVERHEAD_SECS,
            ),
            out_dir,
        )
        try:
            output = read_message(out_dir / PHASE_OUTPUT_FILE, phase_pb.PhaseOutput)
        except SchemaFileError as error:
            msg = f"phase {index} ({load.describe()}) left no usable output: {error}"
            raise BenchmarkError(msg, partial=self.partial) from error
        # Every write sent stays in the history, from a failed or exhausted phase too: the
        # application may have applied it.
        self._history.extend(output.writes)
        self._log(
            f"phase {index}: generated in {output.generate_secs:.1f} s, checked {output.checked} "
            f"in {output.check_secs:.1f} s, {output.invalid} failed, {len(output.writes)} writes"
        )
        try:
            return index, judge(output, validity=self._validity, operations=self._operations)
        except ValueError as error:
            msg = f"phase {index}: {error}"
            raise BenchmarkError(msg, partial=self.partial) from error


class Run(vibesys_evaluator.Run):
    """The sample a microservices ``benchmark(run)`` receives: load phases and the report.

    Each load phase runs against the deployed system and returns what it
    measured, or ends the sample with the reason it is not a measurement.
    Writes carry from phase to phase: every phase sees the earlier ones' writes.
    """

    __slots__ = ("_driver",)
    _driver: PhaseDriver

    def closed_loop(
        self, *, clients: int, secs: float, warmup: float, max_ops_per_sec: float = 20000.0
    ) -> PhaseResult:
        """Capacity: ``clients`` clients, each sending its next operation when the last completed.

        The plan holds ``max_ops_per_sec * (warmup + secs)`` operations; a
        phase that runs out of them is run again once with a plan sized from
        the rate it reached, then fails.

        Args:
            clients: The number of clients, at least 1.
            secs: The measurement window, seconds.
            warmup: Seconds of load before the window.
            max_ops_per_sec: The highest throughput the plan provides for.

        Returns:
            The window's throughput and latency (end minus start).
        """
        if isinstance(clients, bool) or not isinstance(clients, int) or clients < 1:
            msg = f"closed_loop clients must be an int >= 1, got {clients!r}"
            raise ValueError(msg)
        return self._driver.phase(
            Load(
                model=pb.LoadModel.CLOSED,
                clients=clients,
                secs=_positive("closed_loop secs", secs),
                warmup=_warmup(warmup),
                max_ops_per_sec=_positive("closed_loop max_ops_per_sec", max_ops_per_sec),
            )
        )

    def open_loop(
        self,
        *,
        rate: float,
        secs: float,
        warmup: float,
        arrivals: Literal["poisson", "uniform"] = "poisson",
    ) -> PhaseResult:
        """Latency at a fixed offered rate: operations arrive on schedule, whatever the responses.

        Args:
            rate: Operations per second offered.
            secs: The measurement window, seconds.
            warmup: Seconds of load before the window.
            arrivals: ``poisson`` (exponential gaps) or ``uniform`` (every 1/rate seconds).

        Returns:
            The window's throughput and latency (end minus scheduled arrival).
        """
        if arrivals not in ("poisson", "uniform"):
            msg = f"open_loop arrivals is 'poisson' or 'uniform', got {arrivals!r}"
            raise ValueError(msg)
        return self._driver.phase(
            Load(
                model=pb.LoadModel.OPEN,
                rate=_positive("open_loop rate", rate),
                secs=_positive("open_loop secs", secs),
                warmup=_warmup(warmup),
                arrivals=pb.Arrivals.POISSON if arrivals == "poisson" else pb.Arrivals.UNIFORM,
            )
        )


def new_run(sample: Sample, driver: PhaseDriver) -> Run:
    """The ``Run`` a microservices benchmark function receives."""
    run = Run.__new__(Run)
    bind(run, sample)
    run._driver = driver
    return run
