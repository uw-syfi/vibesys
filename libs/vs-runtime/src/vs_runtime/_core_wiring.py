"""The old shell's bindings over the production executors; deleted with the shell.

Nothing in production calls ``core_bindings``: the step loop takes the executors directly
(``core_executors``). Only the old shell's tests still build its ``CoreRuntimeBindings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_runtime._core_executors import (
    MeasurementServices,
    SessionServices,
    core_executors,
    empty_catalog,
)
from vs_runtime._core_loop import CoreRuntimeBindings

if TYPE_CHECKING:
    from vs_project.api import StateNamespace
    from vs_runtime._operation_catalog import OperationCatalog
    from vs_runtime._workspaces import RuntimeWorkspaces
    from vs_sim.api import BlockingRunner, Threads


def core_bindings(  # noqa: PLR0913  # lint-waiver: LW-263518 [PLR0913]; the same keyword arguments as ``core_executors``, which this wraps for the shell that has not been removed yet.
    # > A settings object would be built at each call site and unpacked in the same line, and
    # > the shell this serves is deleted in the next change, so the wrapper stays a plain call.
    *,
    receipts: StateNamespace,
    workspaces: RuntimeWorkspaces,
    measurement: MeasurementServices,
    sessions: SessionServices | None = None,
    threads: Threads,
    blocking: BlockingRunner,
    operations: OperationCatalog | None = None,
) -> CoreRuntimeBindings:
    """The old shell's bindings over ``core_executors``."""
    catalog = operations or empty_catalog()
    bound = core_executors(
        receipts=receipts,
        workspaces=workspaces,
        measurement=measurement,
        sessions=sessions,
        threads=threads,
        blocking=blocking,
        operations=catalog,
    )
    return CoreRuntimeBindings(
        registry=catalog.registry,
        executors=bound.executors,
        steering=None if bound.sessions is None else bound.sessions.turns,
    )
