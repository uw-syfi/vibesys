"""Execution contexts for runtime executor tests: derived payload digest, a revocable lease."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vs_core.api import HostFence, HostId
from vs_runtime.api.core import ExecutionContext

if TYPE_CHECKING:
    from vs_core.api import RequestBase


class RevocableLease:
    """A host lease the test can revoke."""

    def __init__(self) -> None:
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
    """The context a shell would build for *request* on one host."""
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root=host), epoch=epoch),
        now_at=now_at,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        lease=lease or RevocableLease(),
    )
