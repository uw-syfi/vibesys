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
    from vibesys_evaluator.microservices._private.calls import Call

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
#: The share of a read-only operation's responses that are checked by default.
DEFAULT_READ_SAMPLE = 0.1


@dataclass(frozen=True, kw_only=True)
class Operation(Generic[M]):
    """One operation of the mix.

    Attributes:
        name: Unique in the workload: lowercase letters, digits and ``_``.
        weight: Its share of the mix, relative to the other weights.
        build: Builds one request from the model and the operation's own stream.
        check: Says what is wrong with a response (None when it is right),
            after the phase, over the validation sample.
        apply: Makes the operation a write: records it in the model, pending
            while the phase is generated and with its outcome afterwards.
        sample: The share of operations checked: 1.0 for writes (no lower),
            0.1 for other operations by default.
    """

    name: str
    weight: float
    build: Callable[[M, Rand], Request]
    check: Callable[[M, Call], str | None] | None = None
    apply: Callable[[M, Call], None] | None = None
    sample: float | None = None

    def __post_init__(self) -> None:
        """Reject a name, weight or sample the plan cannot carry."""
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            msg = f"Operation name {self.name!r} must match {_NAME.pattern}"
            raise ValueError(msg)
        if isinstance(self.weight, bool) or not (self.weight > 0 and math.isfinite(self.weight)):
            msg = f"Operation {self.name}: weight must be a finite number > 0, got {self.weight!r}"
            raise ValueError(msg)
        if self.sample is not None:
            if not 0.0 <= self.sample <= 1.0:
                msg = f"Operation {self.name}: sample must be in [0, 1], got {self.sample!r}"
                raise ValueError(msg)
            if self.apply is not None and self.sample < 1.0:
                msg = (
                    f"Operation {self.name}: a write (it has apply) is always checked; "
                    f"drop sample={self.sample!r}"
                )
                raise ValueError(msg)


def checked_share(operation: Operation[M]) -> float:
    """The share of the operation's responses that are checked: 0 without a check."""
    if operation.check is None:
        return 0.0
    if operation.sample is not None:
        return operation.sample
    return 1.0 if operation.apply is not None else DEFAULT_READ_SAMPLE


@dataclass(frozen=True, kw_only=True)
class Workload(Generic[D, M]):
    """The model the operations share, and the operations.

    Attributes:
        model: Builds a phase's model from the dataset and the sample's model
            stream (the same in every phase of a sample).
        operations: The mix; names are unique.
    """

    model: Callable[[D, Rand], M]
    operations: Sequence[Operation[M]]

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
    """A workload written in another language: a program that writes the request stream and checks it.

    The phase pod runs ``command + ["generate", ...]`` (loadgen reads its
    standard output) and then ``command + ["check", ...]``, with the flags of
    ``docs/spec.md``, "Generator command line"; a Go generator is
    ``workload.Main`` of the Go SDK. A first element with a ``/`` that is not
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
