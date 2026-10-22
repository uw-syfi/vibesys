"""Typed host facts required by read-only operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vs_proto.api import host_payloads as pb


class ReadSource(Protocol):
    """Fresh host facts; the caller owns I/O and remaining-budget enforcement."""

    def tasks(self, project: str) -> pb.TaskList:
        """The canonical project's task catalog."""
        ...

    def harnesses(self) -> list[pb.HarnessOption]:
        """Vetted harnesses in display order, installed providers first."""
        ...

    def installed_harnesses(self) -> tuple[str, ...]:
        """The providers observed on the host launch PATH."""
        ...

    def examples(self, *, with_catalogue: bool) -> pb.ExampleList:
        """The checkout's example catalog, with requested display details."""
        ...

    def task_goal(self, project: str, task: str) -> pb.TaskGoal:
        """Bounded task goal and declared objectives."""
        ...

    def machine(self) -> pb.MachineDescription:
        """The machine's observed hardware, checkout and active-run facts."""
        ...

    def compute(self) -> pb.ComputeSettings:
        """Compute settings, preserving source ownership and read-only flags."""
        ...

    def inspect_destination(self, example: str, destination: str) -> pb.InspectDestinationResponse:
        """What creating ``example`` would find at ``destination``."""
        ...

    def preflight(self, request: pb.PreflightRunRequest) -> pb.PreflightRunResponse:
        """Complete probe facts; the operation projects requested metadata."""
        ...
