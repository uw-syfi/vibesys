"""What runtime executor tests run an executor with: a context and a store."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import Until
from vs_project.api import StateNamespace
from vs_project.api.testing import scratch_state_directory
from vs_runtime._core_requests import payload_digest_of
from vs_runtime.api.core import ExecutionContext, KeyedDocuments
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import RequestBase


def context_for(
    request: RequestBase, *, now_at: float = 5.0, repeat: bool = False
) -> ExecutionContext:
    """The context the step runner builds for *request*, ending 600 s later.

    With *repeat*, an earlier attempt of the request may have run, as for any attempt after
    the first.
    """
    return ExecutionContext(
        now_at=now_at,
        until=Until(at=now_at + 600.0),
        payload_digest=payload_digest_of(request),
        repeat=repeat,
    )


def receipt_store_in(directory: Path) -> KeyedDocuments:
    """A receipt store over a scratch state directory below *directory*, as a run's has."""
    state = scratch_state_directory(directory, "receipts")
    namespace = StateNamespace(project_root=directory, root=state, portable=False)
    return KeyedDocuments(namespace, threads=SimThreads())
