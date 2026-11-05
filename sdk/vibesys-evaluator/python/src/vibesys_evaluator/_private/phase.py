"""One phase: the phase pod's run, and the Python front end's generator (``generate``, ``check``).

The phase pod (``run_pod``) runs two programs: loadgen, which starts the
task's request generator and reads the request stream from its standard
output while it measures, then the generator's ``check``
(``docs/spec.md``, "Generator command line"). For a Python workload the
generator is the task script itself; this module is its implementation:

1. Model: ``workload.model(data, Rand(seed, "model"))``, then the sample's
   history replayed through each write's ``apply``.
2. Generate: per operation ``i`` of the stream, the operation is drawn by
   weight from the phase stream ``Rand(seed, "phase", index)``, then whether
   it is in the validation sample; ``build`` gets ``phase.child(i)``. A
   write's ``apply`` runs at once with a pending ``Call``. The sampled
   operations and the writes are kept (``KeptOperation``) in the state
   directory. The stream has no end: loadgen closes it.
3. Check: a fresh model, the history and this phase's sent writes applied in
   start order, then ``check`` on every sampled operation that loadgen
   counted as a success, in start order. Results are read one at a time and
   only those of writes and sampled operations are kept.
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING, Any

from vibesys_evaluator._private import dataset as dataset_file
from vibesys_evaluator._private.calls import Call, Response
from vibesys_evaluator._private.deploy import PHASE_INPUT_FILE, PHASE_OUTPUT_FILE
from vibesys_evaluator._private.files import (
    DelimitedWriter,
    SchemaFileError,
    iter_results,
    read_message,
    read_records,
    read_summary,
    write_message,
)
from vibesys_evaluator._private.http import Request, planned, request_info
from vibesys_evaluator._private.loadgen import STEP_TIMEOUT_SECS, PhaseFiles, phase_args
from vibesys_evaluator._private.rand import Rand
from vibesys_evaluator._private.workload import checked_share
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence
    from pathlib import Path

    from vibesys_evaluator._private.loadgen import Commands, Loadgen
    from vibesys_evaluator._private.workload import Operation, Workload

#: The results file left in the work directory (gzip of the loadgen file, kept as an artifact).
RESULTS_FILE = "results.binpb.gz"
SUMMARY_FILE = "summary.binpb"
#: The generator's record of what it must check, in its state directory.
KEPT_FILE = "kept.binpb"
#: The dataset file the phase pod writes for the generator (``--dataset``).
DATASET_FILE = "dataset.json"
MAX_VIOLATIONS = 20
_MAX_MESSAGE = 2000
_NANOS = 1e9
#: Margin on loadgen's own run time: the generator's start-up, the spill after the window, files.
_LOADGEN_MARGIN_SECS = 300.0
#: The most the generator's check may take.
CHECK_TIMEOUT_SECS = 900.0


class PhaseError(Exception):
    """The phase could not produce an output (the message names the operation or file)."""


def phase_rand(phase: phase_pb.PhaseInput) -> Rand:
    """The phase stream."""
    return Rand(phase.sample_seed, "phase", phase.phase_index)


def _info_json(info: Any, operation: str) -> str:
    try:
        return json.dumps(info, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        msg = f"operation {operation}: Request info is not JSON ({error})"
        raise PhaseError(msg) from error


def history_call(entry: phase_pb.HistoryEntry) -> Call:
    """The ``Call`` an ``apply`` receives for a write of an earlier phase (or this one)."""
    return Call(
        info=json.loads(entry.info_json or "{}"),
        start=entry.start_unix_nanos / _NANOS,
        end=entry.end_unix_nanos / _NANOS,
        ok=entry.ok,
        responses=tuple(_response(s) for s in entry.steps),
    )


def _response(step: pb.StepResult) -> Response:
    body = step.response.value if step.response is not None else b""
    return Response(status=step.status, body=body if isinstance(body, bytes) else b"")


def _apply(operation: Operation[Any], model: object, call: Call) -> None:
    if operation.apply is None:
        msg = f"operation {operation.name} is not a write (it has no apply)"
        raise PhaseError(msg)
    try:
        operation.apply(model, call)
    except Exception as error:
        msg = f"operation {operation.name}: apply raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error


def replay(
    workload: Workload[Any, Any], model: object, entries: Iterable[phase_pb.HistoryEntry]
) -> None:
    """Apply each write of ``entries`` (in their order) to ``model``."""
    by_name = {op.name: op for op in workload.operations}
    for entry in entries:
        operation = by_name.get(entry.operation)
        if operation is None or operation.apply is None:
            msg = f"the history has a write of {entry.operation!r}, which is not a write of this workload"
            raise PhaseError(msg)
        _apply(operation, model, history_call(entry))


def build_model(workload: Workload[Any, Any], data: object, phase: phase_pb.PhaseInput) -> object:
    """A fresh model with the sample's history replayed."""
    try:
        model = workload.model(data, Rand(phase.sample_seed, "model"))
    except Exception as error:
        msg = f"Workload model raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error
    replay(workload, model, phase.history)
    return model


def _build(operation: Operation[Any], model: object, rand: Rand) -> Request:
    try:
        request = operation.build(model, rand)
    except Exception as error:
        msg = f"operation {operation.name}: build raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error
    if not isinstance(request, Request):
        msg = f"operation {operation.name}: build returned {type(request).__name__}, not a Request"
        raise PhaseError(msg)
    return request


def requests_header(workload: Workload[Any, Any]) -> pb.RequestsHeader:
    """The stream's ``RequestsHeader``."""
    return pb.RequestsHeader(operation_names=[op.name for op in workload.operations])


def requests(
    workload: Workload[Any, Any],
    model: object,
    phase: phase_pb.PhaseInput,
    keep: Callable[[phase_pb.KeptOperation], None],
) -> Iterator[pb.PlannedOperation]:
    """The phase's operations, without end; ``keep`` gets each sampled operation and write first.

    Raises:
        PhaseError: An operation's ``build`` or ``apply`` failed.
    """
    operations = list(workload.operations)
    weights = [op.weight for op in operations]
    shares = [checked_share(op) for op in operations]
    stream = phase_rand(phase)
    index = 0
    while True:
        choice = stream.weighted(weights)
        sampled = stream.random() < shares[choice]
        operation = operations[choice]
        request = _build(operation, model, stream.child(index))
        write = operation.apply is not None
        if sampled or write:
            info = _info_json(request_info(request), operation.name)
            keep(
                phase_pb.KeptOperation(
                    index=index, operation=choice, info_json=info, sampled=sampled
                )
            )
            if write:
                pending = Call(info=json.loads(info), start=None, end=None, ok=None, responses=())
                _apply(operation, model, pending)
        yield planned(request, choice, keep_bodies=sampled or write)
        index += 1


def generate(
    workload: Workload[Any, Any],
    data: object,
    phase: phase_pb.PhaseInput,
    *,
    out: IO[bytes],
    state: Path,
) -> int:
    """The generator's ``generate``: write the request stream to ``out`` until it is closed.

    Every sampled operation and write is kept in ``state`` before it is
    written to ``out``, so every operation loadgen reads has its record.
    Returns how many operations were written when ``out`` was closed.

    Raises:
        PhaseError: The model, an operation's ``build`` or ``apply`` failed.
    """
    model = build_model(workload, data, phase)
    writer = DelimitedWriter(out)
    written = 0
    with (state / KEPT_FILE).open("wb") as kept:
        keeper = DelimitedWriter(kept)
        try:
            writer.write_header(pb.FileKind.REQUESTS)
            writer.write(requests_header(workload))
            for operation in requests(workload, model, phase, keeper.write):
                writer.write(operation)
                written += 1
        except BrokenPipeError:  # loadgen closed the stream: the phase is over
            return written
    raise AssertionError  # unreachable: the stream has no end


def read_kept(path: Path) -> dict[int, phase_pb.KeptOperation]:
    """The generator's kept operations by index."""
    try:
        return {record.index: record for record in read_records(path, phase_pb.KeptOperation)}
    except SchemaFileError as error:
        raise PhaseError(str(error)) from error


def _call(result: pb.OperationResult, info: dict[str, Any]) -> Call:
    return Call(
        info=info,
        start=result.start_unix_nanos / _NANOS,
        end=result.end_unix_nanos / _NANOS,
        ok=result.error == pb.ErrorKind.UNSPECIFIED,
        responses=tuple(_response(s) for s in result.steps),
    )


def _in_window(result: pb.OperationResult, summary: pb.PhaseSummary) -> bool:
    """Whether loadgen counts the operation in the window: it was scheduled in it."""
    at = result.scheduled_unix_nanos
    return summary.window_start_unix_nanos <= at < summary.window_end_unix_nanos


@dataclass
class _Checked:
    checked: int = 0
    invalid: int = 0
    window: list[phase_pb.Violation] = field(default_factory=list)
    warmup: list[phase_pb.Violation] = field(default_factory=list)

    def add(self, operation: str, index: int, problem: str | None, *, window: bool) -> None:
        """Count one check (in the window) and keep the first violations of each kind."""
        if window:
            self.checked += 1
        if problem is None:
            return
        if window:
            self.invalid += 1
            kept, message = self.window, problem
        else:
            kept, message = self.warmup, "(warmup) " + problem
        if len(kept) < MAX_VIOLATIONS:
            kept.append(
                phase_pb.Violation(operation=operation, index=index, message=message[:_MAX_MESSAGE])
            )


def _sent(
    operations: Sequence[Operation[Any]],
    kept: dict[int, phase_pb.KeptOperation],
    results: Iterable[pb.OperationResult],
) -> list[tuple[pb.OperationResult, phase_pb.KeptOperation]]:
    """The writes loadgen sent and the sampled operations that succeeded, in start order.

    An operation loadgen never sent (start 0: due but not started before the
    phase's deadline) cannot have taken effect, so it is not a write.
    """
    out: list[tuple[pb.OperationResult, phase_pb.KeptOperation]] = []
    for result in results:
        if result.operation >= len(operations):
            msg = f"result {result.index} is of operation {result.operation}, not in the workload"
            raise PhaseError(msg)
        record = kept.get(result.index)
        write = operations[result.operation].apply is not None
        if write and record is None:
            msg = f"no kept record of write {result.index} (the state is not this phase's)"
            raise PhaseError(msg)
        if record is None or result.start_unix_nanos == 0:
            continue
        if record.operation != result.operation:
            msg = (
                f"result {result.index} is operation {result.operation}, the generator kept "
                f"{record.operation} (the state is not this phase's)"
            )
            raise PhaseError(msg)
        if write or (record.sampled and result.error == pb.ErrorKind.UNSPECIFIED):
            out.append((result, record))
    out.sort(key=lambda pair: (pair[0].start_unix_nanos, pair[0].index))
    return out


def check(
    workload: Workload[Any, Any],
    data: object,
    phase: phase_pb.PhaseInput,
    kept: dict[int, phase_pb.KeptOperation],
    summary: pb.PhaseSummary,
    results: Iterable[pb.OperationResult],
) -> phase_pb.PhaseOutput:
    """Check the sample and collect this phase's writes (``results`` may be a stream)."""
    operations = list(workload.operations)
    calls = _sent(operations, kept, results)
    writes = [
        phase_pb.HistoryEntry(
            operation=operations[result.operation].name,
            info_json=record.info_json,
            start_unix_nanos=result.start_unix_nanos,
            end_unix_nanos=result.end_unix_nanos,
            ok=result.error == pb.ErrorKind.UNSPECIFIED,
            steps=list(result.steps),
        )
        for result, record in calls
        if operations[result.operation].apply is not None
    ]
    model = build_model(workload, data, phase)
    replay(workload, model, writes)
    tally = _Checked()
    for result, record in calls:
        operation = operations[result.operation]
        if (
            not record.sampled
            or result.error != pb.ErrorKind.UNSPECIFIED
            or operation.check is None
        ):
            continue
        call = _call(result, json.loads(record.info_json))
        try:
            problem = operation.check(model, call)
        except Exception as error:
            problem = f"check raised {type(error).__name__}: {error}"
        tally.add(
            operation.name,
            result.index,
            None if problem is None else str(problem),
            window=_in_window(result, summary),
        )
    return phase_pb.PhaseOutput(
        summary=summary,
        checked=tally.checked,
        invalid=tally.invalid,
        violations=(tally.window + tally.warmup)[:MAX_VIOLATIONS],
        writes=writes,
    )


def _results(path: Path) -> Iterator[pb.OperationResult]:
    try:
        yield from iter_results(path)
    except SchemaFileError as error:
        raise PhaseError(str(error)) from error


def check_files(
    workload: Workload[Any, Any],
    data: object,
    phase: phase_pb.PhaseInput,
    *,
    state: Path,
    results: Path,
    summary: Path,
    output: Path,
    clock: Callable[[], float],
) -> phase_pb.PhaseOutput:
    """The generator's ``check``: read loadgen's files, check, write the ``PhaseOutput``.

    Raises:
        PhaseError: A file is missing or invalid, or the model or an ``apply`` failed.
    """
    began = clock()
    try:
        loadgen_summary = read_summary(summary)
    except SchemaFileError as error:
        raise PhaseError(str(error)) from error
    out = check(
        workload, data, phase, read_kept(state / KEPT_FILE), loadgen_summary, _results(results)
    )
    out.check_secs = clock() - began
    write_message(output, out)
    return out


@dataclass(frozen=True, kw_only=True)
class PodTask:
    """What the task's script gives the phase pod.

    Attributes:
        dataset: Builds the dataset (written to the file the generator reads).
        generator: The generator command (``generate``/``check`` and their flags follow).
        min_sustained_ratio: The open loop's ``--min-sustained``.
    """

    dataset: Callable[[], object]
    generator: Sequence[str]
    min_sustained_ratio: float


def run_pod(
    task: PodTask,
    *,
    work: Path,
    scratch: Path,
    target: str,
    loadgen: Loadgen,
    commands: Commands,
) -> None:
    """Run the phase whose input is in ``work`` and leave its output there.

    loadgen starts the generator's ``generate``; ``commands`` runs its ``check``.

    Raises:
        PhaseError: The dataset, loadgen (and the generator under it) or the check failed.
    """
    input_path = work / PHASE_INPUT_FILE
    phase = read_message(input_path, phase_pb.PhaseInput)
    try:
        encoded = dataset_file.encode(task.dataset())
    except Exception as error:
        msg = f"dataset raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error
    (scratch / DATASET_FILE).write_bytes(encoded)
    state = scratch / "state"
    state.mkdir()
    files = PhaseFiles(results=scratch / "results.binpb", summary=work / SUMMARY_FILE)
    shared = [
        "--phase-input",
        str(input_path),
        "--dataset",
        str(scratch / DATASET_FILE),
        "--state",
        str(state),
    ]
    args = phase_args(
        model=phase.model,
        target=target,
        files=files,
        secs=phase.secs,
        warmup_secs=phase.warmup_secs,
        clients=phase.clients,
        rate=phase.rate,
        arrivals=phase.arrivals,
        arrivals_seed=phase_rand(phase).child("arrivals").below(1 << 63),
        min_sustained=task.min_sustained_ratio,
        generator=[*task.generator, "generate", *shared],
    )
    budget = phase.warmup_secs + phase.secs + 2 * STEP_TIMEOUT_SECS + _LOADGEN_MARGIN_SECS
    done = loadgen.run(args, timeout_secs=budget)
    if done.code != 0:
        msg = f"loadgen exited {done.code}: {done.stderr.strip()[-_MAX_MESSAGE:]}"
        raise PhaseError(msg)
    checked = commands.run(
        [
            *task.generator,
            "check",
            *shared,
            "--results",
            str(files.results),
            "--summary",
            str(files.summary),
            "--output",
            str(work / PHASE_OUTPUT_FILE),
        ],
        timeout_secs=CHECK_TIMEOUT_SECS,
    )
    if checked.code != 0:
        msg = (
            f"the generator's check exited {checked.code}: {checked.stderr.strip()[-_MAX_MESSAGE:]}"
        )
        raise PhaseError(msg)
    with files.results.open("rb") as raw, gzip.open(work / RESULTS_FILE, "wb") as packed:
        shutil.copyfileobj(raw, packed)
    files.results.unlink()
