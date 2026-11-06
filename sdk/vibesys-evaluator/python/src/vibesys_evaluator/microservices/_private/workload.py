"""``Operation`` and ``Workload``: the weighted mix of operations and the model they share."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic

from vibesys_evaluator._private.typevars import D, M

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys_evaluator._private.http import Request
    from vibesys_evaluator._private.rand import Rand

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True, kw_only=True)
class Operation(Generic[M]):
    """One operation of the mix.

    Attributes:
        name: Unique in the workload: lowercase letters, digits and ``_``.
        weight: Its share of the mix, relative to the other weights.
        build: Builds one request from the model and the operation's own
            stream. It may change the model, so that later operations build
            on it (an update, then a read of the new value).
    """

    name: str
    weight: float
    build: Callable[[M, Rand], Request]

    def __post_init__(self) -> None:
        """Reject a name or weight the plan cannot carry."""
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            msg = f"Operation name {self.name!r} must match {_NAME.pattern}"
            raise ValueError(msg)
        if isinstance(self.weight, bool) or not (self.weight > 0 and math.isfinite(self.weight)):
            msg = f"Operation {self.name}: weight must be a finite number > 0, got {self.weight!r}"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class Workload(Generic[D, M]):
    """The model the operations share, and the operations.

    Attributes:
        model: Builds a phase's model from the dataset and the sample's model
            stream (the same in every phase of a sample). Every phase starts
            from a model built afresh.
        operations: The mix; names are unique.
        begin_phase: Called once per phase with the model and the phase's
            index (0 for the sample's first), before the first ``build``: a
            workload whose requests depend on the phase keeps the index in
            its model. None when they do not.
    """

    model: Callable[[D, Rand], M]
    operations: Sequence[Operation[M]]
    begin_phase: Callable[[M, int], None] | None = None

    def __post_init__(self) -> None:
        """Reject an empty mix or a repeated name."""
        if not self.operations:
            msg = "Workload needs at least one operation"
            raise ValueError(msg)
        seen: set[str] = set()
        for operation in self.operations:
            if not isinstance(operation, Operation):
                msg = f"Workload operations are Operation, got {type(operation).__name__}"
                raise TypeError(msg)
            if operation.name in seen:
                msg = f"Workload has two operations named {operation.name!r}"
                raise ValueError(msg)
            seen.add(operation.name)


@dataclass(frozen=True)
class Generator:
    """A workload written in another language: a program that writes the request stream.

    The phase pod runs ``command + ["generate", ...]`` (loadgen reads its
    standard output), with the flags of ``docs/spec.md``, "Generator command
    line"; a Go generator is ``workload.Main`` of the Go SDK. A first element with a ``/`` that is not
    absolute is relative to the task script's directory; one without is
    looked up on ``PATH``.

    Attributes:
        command: The program and its leading arguments.
    """

    command: Sequence[str]

    def __post_init__(self) -> None:
        """Reject an empty command or one that is not a sequence of strings."""
        if isinstance(self.command, str):
            msg = f"Generator command is a list of arguments, not the string {self.command!r}"
            raise TypeError(msg)
        command = tuple(self.command)
        if not command or not all(isinstance(arg, str) and arg for arg in command):
            msg = f"Generator command must be non-empty strings, got {self.command!r}"
            raise ValueError(msg)
        object.__setattr__(self, "command", command)
