"""The Python front end's generator: ``generate``, the request stream loadgen reads in a phase.

For a Python workload the generator is the task script itself
(``docs/spec.md``, "Generator command line"):

1. Model: ``workload.model(data, Rand(seed, "model"))``, then
   ``workload.begin_phase(model, index)``.
2. Generate: per operation ``i`` of the stream, the operation is drawn by
   weight from the phase stream ``Rand(seed, "phase", index)``; ``build``
   gets ``phase.child(i)``. The stream has no end: loadgen closes it.

Under accuracy capture (``PhaseInput.capture``) every operation keeps its
response bodies and is first kept (``KeptOperation``, with its info) in the
state directory. The Go SDK's ``workload.Main`` writes the same stream.
"""

from __future__ import annotations

import json
from typing import IO, TYPE_CHECKING, Any

from vibesys_evaluator._private.files import DelimitedWriter
from vibesys_evaluator._private.http import Request, planned, request_info
from vibesys_evaluator._private.rand import Rand
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from vibesys_evaluator.microservices._private.workload import Operation, Workload

#: The generator's record of every operation under capture, in its state directory.
KEPT_FILE = "kept.binpb"


class PhaseError(Exception):
    """The generator could not write the stream (the message names the operation)."""


def phase_rand(phase: phase_pb.PhaseInput) -> Rand:
    """The phase stream."""
    return Rand(phase.sample_seed, "phase", phase.phase_index)


def _info_json(info: Any, operation: str) -> str:
    try:
        return json.dumps(info, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        msg = f"operation {operation}: Request info is not JSON ({error})"
        raise PhaseError(msg) from error


def build_model(workload: Workload[Any, Any], data: object, phase: phase_pb.PhaseInput) -> object:
    """The phase's model: built afresh, then told the phase."""
    try:
        model = workload.model(data, Rand(phase.sample_seed, "model"))
    except Exception as error:
        msg = f"Workload model raised {type(error).__name__}: {error}"
        raise PhaseError(msg) from error
    if workload.begin_phase is not None:
        try:
            workload.begin_phase(model, phase.phase_index)
        except Exception as error:
            msg = f"Workload begin_phase raised {type(error).__name__}: {error}"
            raise PhaseError(msg) from error
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
    keep: Callable[[phase_pb.KeptOperation], None] | None,
) -> Iterator[pb.PlannedOperation]:
    """The phase's operations, without end; under capture ``keep`` gets each one first.

    Raises:
        PhaseError: An operation's ``build`` failed.
    """
    operations = list(workload.operations)
    weights = [op.weight for op in operations]
    stream = phase_rand(phase)
    capture = keep is not None
    index = 0
    while True:
        choice = stream.weighted(weights)
        operation = operations[choice]
        request = _build(operation, model, stream.child(index))
        if keep is not None:
            info = _info_json(request_info(request), operation.name)
            keep(phase_pb.KeptOperation(index=index, operation=choice, info_json=info))
        yield planned(request, choice, keep_bodies=capture)
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

    Under capture every operation is kept in ``state`` before it is written to
    ``out``, so every operation loadgen reads has its record. Returns how many
    operations were written when ``out`` was closed.

    Raises:
        PhaseError: The model or an operation's ``build`` failed.
    """
    model = build_model(workload, data, phase)
    writer = DelimitedWriter(out)
    written = 0
    kept = (state / KEPT_FILE).open("wb") if phase.capture else None
    try:
        keep = DelimitedWriter(kept).write if kept is not None else None
        writer.write_header(pb.FileKind.REQUESTS)
        writer.write(requests_header(workload))
        for operation in requests(workload, model, phase, keep):
            writer.write(operation)
            written += 1
    except BrokenPipeError:  # loadgen closed the stream: the phase is over
        return written
    finally:
        if kept is not None:
            kept.close()
    raise AssertionError  # unreachable: the stream has no end
