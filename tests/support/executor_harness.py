"""One crash, authority and observation harness for every receipt-backed executor kind.

A case wires one executor to its real owners over a real Project state directory and
exposes scenarios, one per request kind it executes. The shared tests in
``libs/vs-runtime/tests/test_executor_contract.py`` run every scenario of every
registered case through the same checks:

* crash at every durable-write boundary (before and after each write), then a real
  restart over the same disk: the effect happens exactly once and the observations
  core receives are accepted in order;
* a stale host (lost lease, older fence) performs zero effects and reports Unknown;
* the observation contract (``assert_core_accepts``) over Unknown-then-final and replays;
* the same request identity with another payload is a REJECTED observation;
* ``InspectRequest`` of the request, at every crash boundary, never says "never started"
  once an effect happened, and reports the sealed result once the request finished.

A new executor kind adds a scenario here and gets all of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from pydantic import BaseModel
    from tests.support.executor_context import RevocableLease

    from vs_core.api import RequestBase
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import ExecutionResult


class ProcessKilledError(
    BaseException
):  # lint-waiver: LW-0D3-8 [N818]; a BaseException subclass models a process kill that no executor may catch.
    """The process died at this durable-write boundary."""


class FaultingNamespace:
    """A real state namespace that kills the process before or after chosen writes.

    Boundary ``2 * i`` is just before write ``i`` and ``2 * i + 1`` just after it.
    ``writes`` counts the writes performed, so a crash-free run tells how many
    boundaries exist.
    """

    def __init__(self, real: StateNamespace, crash_at: int | None = None) -> None:
        self._real = real
        self._crash_at = crash_at
        self.writes = 0

    def _boundary(self, index: int) -> None:
        if self._crash_at == index:
            raise ProcessKilledError

    def save(self, relative_path: str, model: BaseModel) -> None:
        """Write through, with a kill boundary on each side."""
        self._boundary(2 * self.writes)
        self._real.save(relative_path, model)
        self._boundary(2 * self.writes + 1)
        self.writes += 1

    def write_bytes(self, relative_path: str, contents: bytes) -> None:
        """Write through, with a kill boundary on each side."""
        self._boundary(2 * self.writes)
        self._real.write_bytes(relative_path, contents)
        self._boundary(2 * self.writes + 1)
        self.writes += 1

    def __getattr__(self, name: str) -> object:
        """Every other namespace method is the real one."""
        return getattr(self._real, name)


@dataclass(frozen=True)
class Scenario:
    """One request kind of one executor.

    ``effectful`` requests change the world, so they need host authority and seal
    their result (a stale host reports Unknown; another payload is REJECTED).
    """

    name: str
    kind: type[RequestBase]
    effectful: bool


class CaseWorld(Protocol):
    """One scenario's durable world: owners, disk and the executor that runs over them."""

    async def prepare(self, scenario: Scenario) -> RequestBase:
        """Seed whatever the scenario's request refers to, and return the request."""
        ...

    async def execute(
        self,
        request: RequestBase,
        *,
        lease: RevocableLease,
        crash_at: int | None,
        digest: str | None = None,
    ) -> ExecutionResult:
        """Run the request on a freshly built executor over the same disk (a restart)."""
        ...

    def writes(self) -> int:
        """Durable writes the last ``execute`` performed."""
        ...

    def receipts_namespace(self) -> StateNamespace:
        """The real namespace the executor's ReceiptStore writes to (no fault injection)."""
        ...

    def owners_root(self) -> Path:
        """The directory of the registered operation owners, so inspection sees their effects."""
        ...

    def effects(self) -> int:
        """Real effects the owners have performed so far, counted on durable state."""
        ...


class ExecutorCase(Protocol):
    """A registered executor: its request kinds, scenarios, and a world to run them in."""

    name: str
    scenarios: tuple[Scenario, ...]

    def world(self) -> AbstractAsyncContextManager[CaseWorld]:
        """A fresh world in its own temporary directory."""
        ...
