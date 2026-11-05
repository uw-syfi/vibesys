"""One phase, as the phase pod runs it: model, generate, loadgen, check, output.

Nothing here runs while loadgen measures: the plan is written before it
starts and the responses are checked after it exits.

1. Model: ``workload.model(data, Rand(seed, "model"))``, then the sample's
   history replayed through each write's ``apply``.
2. Generate: per planned operation ``i``, the operation is drawn by weight
   from the phase stream ``Rand(seed, "phase", index)``, then whether it is
   in the validation sample; ``build`` gets ``phase.child(i)``. A write's
   ``apply`` runs at once with a pending ``Call``.
3. Run loadgen over the plan.
4. Check: a fresh model, the history and this phase's sent writes applied in
   start order, then ``check`` on every sampled operation that loadgen
   counted as a success, in start order. The results file is streamed and
   only the results of writes and sampled operations are kept, so the
   check's memory grows with the sample, not with the plan.
"""

from __future__ import annotations

import gzip
import json
import math
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibesys_evaluator._private.calls import Call, Response
from vibesys_evaluator._private.deploy import PHASE_INPUT_FILE, PHASE_OUTPUT_FILE
from vibesys_evaluator._private.files import (
    DelimitedWriter,
    iter_results,
    read_message,
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
    from collections.abc import Callable, Iterable, Sequence
    from pathlib import Path

    from vibesys_evaluator._private.loadgen import Loadgen
    from vibesys_evaluator._private.workload import Operation, Workload

#: The results file left in the work directory (gzip of the loadgen file, kept as an artifact).
RESULTS_FILE = "results.binpb.gz"
SUMMARY_FILE = "summary.binpb"
MAX_VIOLATIONS = 20
_MAX_MESSAGE = 2000
_NANOS = 1e9
#: Margin on loadgen's own run time: reading the plan, the spill after the window, writing files.
_LOADGEN_MARGIN_SECS = 120.0


class PhaseError(Exception):
    """The phase could not produce an output (the message names the operation or file)."""


def plan_size(phase: phase_pb.PhaseInput) -> int:
    """How many operations the plan holds (``docs/spec.md``, "A phase")."""
    span = phase.warmup_secs + phase.secs
    if phase.model == pb.LoadModel.OPEN:
        return math.ceil(1.02 * phase.rate * span) + 100
    return max(1, math.ceil(phase.max_ops_per_sec * span))


def _sample_rand(seed: int) -> Rand:
    return Rand(seed, "model")


def phase_rand(phase: phase_pb.PhaseInput) -> Rand:
    """The phase stream."""
    return Rand(phase.sample_seed, "phase", phase.phase_index)


def _roundtrip(info: Any, operation: str) -> tuple[str, dict[str, Any]]:
    try:
        text = json.dumps(info, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        msg = f"operation {operation}: Request info is not JSON ({error})"
        raise PhaseError(msg) from error
    return text, json.loads(text)


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
        model = workload.model(data, _sample_rand(phase.sample_seed))
    except Exception as error:
        msg = f"Workload model raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error
    replay(workload, model, phase.history)
    return model


@dataclass
class Plan:
    """What generation remembers about the plan for the check: per operation, its kind and info."""

    operations: list[int] = field(default_factory=list)
    #: Sampled operations and writes: the info's JSON text (parsed again when checked).
    infos: dict[int, str] = field(default_factory=dict)
    sampled: set[int] = field(default_factory=set)


def generate(
    workload: Workload[Any, Any], model: object, phase: phase_pb.PhaseInput, path: Path
) -> Plan:
    """Write the phase's requests file at ``path``."""
    operations = list(workload.operations)
    weights = [op.weight for op in operations]
    shares = [checked_share(op) for op in operations]
    count = plan_size(phase)
    stream = phase_rand(phase)
    plan = Plan()
    with path.open("wb") as file:
        writer = DelimitedWriter(file)
        writer.write_header(pb.FileKind.REQUESTS)
        writer.write(pb.RequestsHeader(operation_names=[op.name for op in operations], count=count))
        for index in range(count):
            choice = stream.weighted(weights)
            sampled = stream.random() < shares[choice]
            operation = operations[choice]
            try:
                request = operation.build(model, stream.child(index))
            except Exception as error:
                msg = f"operation {operation.name}: build raised {type(error).__name__}: {error}"
                raise PhaseError(msg) from error
            if not isinstance(request, Request):
                msg = f"operation {operation.name}: build returned {type(request).__name__}, not a Request"
                raise PhaseError(msg)
            plan.operations.append(choice)
            if sampled or operation.apply is not None:
                info = _roundtrip(request_info(request), operation.name)
                plan.infos[index] = info[0]
                if sampled:
                    plan.sampled.add(index)
                if operation.apply is not None:
                    _apply(
                        operation,
                        model,
                        Call(info=info[1], start=None, end=None, ok=None, responses=()),
                    )
            writer.write(
                planned(request, choice, keep_bodies=sampled or operation.apply is not None)
            )
    return plan


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


def _writes(
    operations: Sequence[Operation[Any]], plan: Plan, results: Sequence[pb.OperationResult]
) -> list[phase_pb.HistoryEntry]:
    """This phase's sent writes, in the order of ``results``.

    An operation loadgen never sent (start 0: due but not started before the
    phase's deadline) cannot have taken effect, so it is not a write.
    """
    return [
        phase_pb.HistoryEntry(
            operation=operation.name,
            info_json=plan.infos[result.index],
            start_unix_nanos=result.start_unix_nanos,
            end_unix_nanos=result.end_unix_nanos,
            ok=result.error == pb.ErrorKind.UNSPECIFIED,
            steps=list(result.steps),
        )
        for result in results
        if result.start_unix_nanos != 0
        and (operation := operations[plan.operations[result.index]]).apply is not None
    ]


def check(
    workload: Workload[Any, Any],
    data: object,
    phase: phase_pb.PhaseInput,
    plan: Plan,
    summary: pb.PhaseSummary,
    results: Iterable[pb.OperationResult],
) -> phase_pb.PhaseOutput:
    """Check the sample and collect this phase's writes.

    Only the results of writes and sampled operations are kept from ``results``
    (which may be a stream): the others are never checked and never applied.
    """
    operations = list(workload.operations)
    by_index = sorted(
        (r for r in results if r.index < len(plan.operations) and r.index in plan.infos),
        key=lambda r: (r.start_unix_nanos, r.index),
    )
    writes = _writes(operations, plan, by_index)
    model = build_model(workload, data, phase)
    replay(workload, model, writes)
    tally = _Checked()
    for result in by_index:
        if result.index not in plan.sampled or result.error != pb.ErrorKind.UNSPECIFIED:
            continue
        operation = operations[plan.operations[result.index]]
        if operation.check is None:  # only operations with a check are sampled
            continue
        call = _call(result, json.loads(plan.infos[result.index]))
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


@dataclass(frozen=True, kw_only=True)
class PodTask:
    """What the task's script gives the phase pod."""

    dataset: Callable[[], object]
    workload: Workload[Any, Any]
    min_sustained_ratio: float


def run_pod(
    task: PodTask,
    *,
    work: Path,
    scratch: Path,
    target: str,
    loadgen: Loadgen,
    clock: Callable[[], float],
) -> None:
    """Run the phase whose input is in ``work`` and leave its output there.

    Raises:
        PhaseError: The model, generation, loadgen or the check failed.
    """
    phase = read_message(work / PHASE_INPUT_FILE, phase_pb.PhaseInput)
    try:
        data = task.dataset()
    except Exception as error:
        msg = f"dataset raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error
    started = clock()
    model = build_model(task.workload, data, phase)
    files = PhaseFiles(
        requests=scratch / "requests.binpb",
        results=scratch / "results.binpb",
        summary=work / SUMMARY_FILE,
    )
    plan = generate(task.workload, model, phase, files.requests)
    generated = clock()
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
    )
    budget = phase.warmup_secs + phase.secs + 2 * STEP_TIMEOUT_SECS + _LOADGEN_MARGIN_SECS
    done = loadgen.run(args, timeout_secs=budget)
    if done.code != 0:
        msg = f"loadgen exited {done.code}: {done.stderr.strip()[-_MAX_MESSAGE:]}"
        raise PhaseError(msg)
    summary = read_summary(files.summary)
    checking = clock()
    output = check(task.workload, data, phase, plan, summary, iter_results(files.results))
    output.generate_secs = generated - started
    output.check_secs = clock() - checking
    with files.results.open("rb") as raw, gzip.open(work / RESULTS_FILE, "wb") as packed:
        shutil.copyfileobj(raw, packed)
    files.results.unlink()
    files.requests.unlink()
    write_message(work / PHASE_OUTPUT_FILE, output)
