"""What runtime executor tests run an executor with: a context, a revocable lease, a store."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_core.api import HostFence, HostId, Until
from vs_project.api import StateNamespace
from vs_project.api.testing import scratch_state_directory
from vs_runtime._core_requests import payload_digest_of
from vs_runtime.api.core import ExecutionContext, ReceiptStore
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vs_core.api import RequestBase


class RevocableLease:
    """A host lease the test can revoke."""

    def __init__(self) -> None:
        """Start with the lease held."""
        self.valid = True

    def renew(self, *, now_at: float, lease_duration: float) -> None:
        """Accept any renewal."""
        del now_at, lease_duration

    def verify(self, *, now_at: float) -> bool:
        """Whether the lease is still held."""
        del now_at
        return self.valid


def context_for(
    request: RequestBase,
    *,
    lease: RevocableLease | None = None,
    epoch: int = 1,
    host: str = "host",
    now_at: float = 5.0,
) -> ExecutionContext:
    """The context a shell would build for *request* on one host, ending 600 s later."""
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root=host), epoch=epoch),
        now_at=now_at,
        until=Until(at=now_at + 600.0),
        payload_digest=payload_digest_of(request),
        lease=lease or RevocableLease(),
    )


def receipt_store_in(directory: Path) -> ReceiptStore:
    """A receipt store over a scratch state directory below *directory*, as a run's has."""
    state = scratch_state_directory(directory, "receipts")
    namespace = StateNamespace(project_root=directory, root=state, portable=False)
    return ReceiptStore(namespace, threads=SimThreads())
