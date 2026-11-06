"""``Run``: the phase primitives ``benchmark(run)`` composes, and the orchestration behind them.

Each phase is one ``PhaseJob`` handed to the deploy layer's ``PhaseRunner``,
whose pod runs the core's ``phase pod``; the core's ``phase judge`` then says
whether the phase is a measurement, and ``BenchmarkError`` carries the reason
when it is not, which ends the sample with an error record. Under accuracy
capture the sample's ``history.binpb`` lists the phases and the faults
(``Run.fault``) beside them.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import vibesys_evaluator
from vibesys_evaluator._private import core
from vibesys_evaluator._private.files import SchemaFileError, read_message
from vibesys_evaluator._private.records import Partial
from vibesys_evaluator._private.results import finite
from vibesys_evaluator._private.run import bind
from vibesys_evaluator._private.sample import SampleError
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private.deploy import PHASE_OUTPUT_FILE, PhaseJob
from vibesys_evaluator.microservices._private.results import PhaseResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys_evaluator._private.results import Validity
    from vibesys_evaluator._private.sample import Sample
    from vibesys_evaluator.microservices._private.deploy import PhaseRunner

#: Time a phase pod may spend outside the window: its start, the generator's, the spill, copying.
PHASE_OVERHEAD_SECS = 1500.0
#: The accuracy history's manifest, in the artifacts (``docs/spec.md``, "Accuracy history").
HISTORY_FILE = "history.binpb"
#: The dataset file in the artifacts.
DATASET_FILE = "dataset.json"
_FAULT_KIND = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_NANOS = 1_000_000_000


class BenchmarkError(SampleError):
    """A phase is not a measurement; the message is the error record's."""


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
    """Runs the sample's phases in order, judges each, and keeps the history under capture."""

    def __init__(
        self,
        *,
        runner: PhaseRunner,
        sample: Sample,
        validity: Validity,
        log: Callable[[str], None],
        capture: bool = False,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        """Drive phases through ``runner``; their files go to the sample's phase directories."""
        self._runner = runner
        self._sample = sample
        self._seed = sample.seed
        self._validity = validity
        self._log = log
        self._capture = capture
        self._clock = clock
        self._next_index = 0
        self._faults: list[phase_pb.FaultEvent] = []
        self._window_end = 0
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

    def _fail(self, message: str) -> BenchmarkError:
        return BenchmarkError(message, partial=self.partial)

    def phase(self, load: Load) -> PhaseResult:
        """Run one phase and judge it."""
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
            capture=self._capture,
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
        if self._capture:
            self._write_history()
        verdict = core.call(
            "phase",
            "judge",
            {
                "phase_dir": str(out_dir),
                "input": json.loads(phase_input.to_json()),
                "validity": {
                    "max_error_rate": self._validity.max_error_rate,
                    "min_sustained_ratio": self._validity.min_sustained_ratio,
                    "min_ops_per_operation": self._validity.min_ops_per_operation,
                },
            },
        )
        for line in verdict.get("log", []):
            self._log(str(line))
        if verdict.get("failure"):
            raise self._fail(str(verdict["failure"]))
        measured = verdict.get("measurement") or {}
        result = PhaseResult(
            ops_per_sec=float(measured.get("ops_per_sec", 0.0)),
            p50_ms=float(measured.get("p50_ms", 0.0)),
            p99_ms=float(measured.get("p99_ms", 0.0)),
        )
        self._window_end = _window_end(out_dir)
        self._last = result
        self._sample.partial = self.partial
        return result

    def fault(self, kind: str, target: str, action: Callable[[], None]) -> None:
        """Run ``action`` between two phases and record it as a fault (see ``Run.fault``)."""
        if not isinstance(kind, str) or not _FAULT_KIND.match(kind):
            msg = f"fault kind {kind!r} must match {_FAULT_KIND.pattern}"
            raise ValueError(msg)
        if self._next_index == 0:
            msg = f"fault {kind}: a fault goes between phases; run a phase first"
            raise ValueError(msg)
        after = self._next_index - 1
        start = self._clock()
        if start < self._window_end:
            self._log(
                f"fault {kind} starts before phase {after}'s window ended on loadgen's clock: this "
                "machine's clock and the phase pod's disagree, and the fault's times are off by as much"
            )
        try:
            action()
        finally:
            end = self._clock()
            self._faults.append(
                phase_pb.FaultEvent(
                    kind=kind,
                    target=str(target),
                    start_unix_nanos=start,
                    end_unix_nanos=end,
                    after_phase=after,
                )
            )
            if self._capture:
                self._write_history()
        self._log(f"fault {kind} on {target} after phase {after}: {(end - start) / _NANOS:.1f} s")

    def _write_history(self) -> None:
        manifest = phase_pb.HistoryManifest(
            phases=self._next_index, faults=list(self._faults), dataset=DATASET_FILE
        )
        path = self._sample.artifacts / HISTORY_FILE
        partial = path.with_name(HISTORY_FILE + ".tmp")
        partial.write_bytes(manifest.to_binary())
        partial.replace(path)


def _window_end(out_dir: Path) -> int:
    """The end of a judged phase's window on loadgen's clock (0 when unknown)."""
    try:
        output = read_message(out_dir / PHASE_OUTPUT_FILE, phase_pb.PhaseOutput)
    except SchemaFileError:
        return 0
    return output.summary.window_end_unix_nanos if output.summary is not None else 0


class Run(vibesys_evaluator.Run):
    """The sample a microservices ``benchmark(run)`` receives: load phases and the report.

    Each load phase runs against the deployed system and returns what it
    measured, or ends the sample with the reason it is not a measurement.
    Every phase builds its requests from a model made afresh; nothing is reset
    between phases, so the application keeps what earlier phases wrote.
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

    def fault(self, kind: str, target: str, action: Callable[[], None]) -> None:
        """Run ``action`` between two phases and record it as a fault.

        No phase runs while it does; its start and end are taken on this
        machine's clock, which must agree with the phase pods' (loadgen stamps
        its results with theirs). Under capture (``main(capture=True)``) the
        faults go into the accuracy history (``history.binpb`` in the
        artifacts). An exception of ``action`` propagates after the fault is
        recorded.

        Args:
            kind: What was done, a name the task's oracle gives meaning
                (``restart``, ``flush``): lowercase letters, digits and ``_``.
            target: What it hit, for messages.
            action: Does it, and returns when it is done.
        """
        self._driver.fault(kind, target, action)


def new_run(sample: Sample, driver: PhaseDriver) -> Run:
    """The ``Run`` a microservices benchmark function receives."""
    run = Run.__new__(Run)
    bind(run, sample)
    run._driver = driver
    return run
