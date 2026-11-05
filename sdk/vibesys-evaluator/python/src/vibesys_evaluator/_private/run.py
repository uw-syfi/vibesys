"""``Run``: the phase primitives ``benchmark(run)`` composes, and the orchestration behind them.

Each phase is one ``PhaseJob`` handed to the deploy layer's ``PhaseRunner``.
The orchestrator keeps the sample's write history (the writes every phase
sent, in start order) and gives it to the next phase, so state carries from
phase to phase; it judges each phase's output and raises ``BenchmarkError``
with the reason, which ends the sample with an error record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vibesys_evaluator._private.deploy import PHASE_OUTPUT_FILE, PhaseJob
from vibesys_evaluator._private.files import SchemaFileError, read_message
from vibesys_evaluator._private.judge import Failed, judge
from vibesys_evaluator._private.records import Partial, RecordError
from vibesys_evaluator._private.results import PhaseResult, finite
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vibesys_evaluator._private.deploy import PhaseRunner
    from vibesys_evaluator._private.records import RecordStream
    from vibesys_evaluator._private.results import Validity

#: Time a phase pod may spend outside the window: the generator's start, the check, copying.
PHASE_OVERHEAD_SECS = 1500.0


class BenchmarkError(Exception):
    """The sample is not a measurement; the message is the error record's."""

    def __init__(self, message: str, *, partial: Partial | None = None) -> None:
        """Carry the message and what was measured before the failure."""
        super().__init__(message)
        self.partial = partial


@dataclass(frozen=True, kw_only=True)
class Load:
    """How one phase offers load."""

    model: pb.LoadModel
    secs: float
    warmup: float
    clients: int = 0
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


class PhaseDriver:
    """Runs the sample's phases in order and keeps its write history."""

    def __init__(
        self,
        *,
        runner: PhaseRunner,
        sample_seed: int,
        validity: Validity,
        artifacts: Path,
        records: RecordStream,
        log: Callable[[str], None],
    ) -> None:
        """Drive phases through ``runner``; their files go under ``artifacts``."""
        self._runner = runner
        self._seed = sample_seed
        self._validity = validity
        self._artifacts = artifacts
        self._records = records
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
        """Run one phase."""
        index, verdict = self._run(load)
        if isinstance(verdict, Failed):
            msg = f"phase {index} ({load.describe()}): {verdict.reason}"
            raise BenchmarkError(msg, partial=self.partial)
        self._log(
            f"phase {index} ({load.describe()}): {verdict.ops_per_sec:.1f} ops/s, "
            f"p50 {verdict.p50_ms:.2f} ms, p99 {verdict.p99_ms:.2f} ms"
        )
        self._last = verdict
        return verdict

    def _run(self, load: Load) -> tuple[int, PhaseResult | Failed]:
        index = self._next_index
        self._next_index += 1
        phase_input = phase_pb.PhaseInput(
            sample_seed=self._seed,
            phase_index=index,
            model=load.model,
            clients=load.clients,
            rate=load.rate,
            arrivals=load.arrivals,
            secs=load.secs,
            warmup_secs=load.warmup,
            history=list(self._history),
        )
        name = f"phase-{index}"
        out_dir = self._artifacts / name
        out_dir.mkdir(parents=True, exist_ok=False)
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
        # Every write sent stays in the history, from a failed phase too: the application
        # may have applied it.
        self._history.extend(output.writes)
        generator = output.summary.generator if output.summary is not None else None
        if generator is not None:
            self._log(
                f"phase {index}: the generator wrote {generator.rate:.0f} ops/s, "
                f"{generator.waited} operations waited for it"
            )
        self._log(
            f"phase {index}: checked {output.checked} in {output.check_secs:.1f} s, "
            f"{output.invalid} failed, {len(output.writes)} writes"
        )
        try:
            return index, judge(output, validity=self._validity)
        except ValueError as error:
            msg = f"phase {index}: {error}"
            raise BenchmarkError(msg, partial=self.partial) from error

    def report(self, values: Mapping[str, float]) -> None:
        """Write the result record."""
        try:
            self._records.result(values)
        except RecordError as error:
            msg = f"run.report: {error}"
            raise ValueError(msg) from error


class Run:
    """The phases of one sample: given to the task's ``benchmark(run)``.

    Each call runs one phase against the deployed system and returns what it
    measured, or ends the sample with the reason it is not a measurement.
    Writes carry from phase to phase: every phase sees the earlier ones' writes.
    """

    __slots__ = ("_driver",)
    _driver: PhaseDriver

    def closed_loop(self, *, clients: int, secs: float, warmup: float) -> PhaseResult:
        """Capacity: ``clients`` clients, each sending its next operation when the last completed.

        The operations come from the workload's request stream, which has no
        end; a phase whose clients wait for it is not a measurement.

        Args:
            clients: The number of clients, at least 1.
            secs: The measurement window, seconds.
            warmup: Seconds of load before the window.

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

    def report(self, values: Mapping[str, float]) -> None:
        """Report the sample's metrics (once): every declared metric, a finite number each.

        Args:
            values: Metric name to value.
        """
        self._driver.report(values)


def new_run(driver: PhaseDriver) -> Run:
    """The ``Run`` a benchmark function receives."""
    run = Run.__new__(Run)
    run._driver = driver
    return run
