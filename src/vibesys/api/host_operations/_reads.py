"""Typed host facts required by read-only operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import host_payloads as pb


class ReadSource(Protocol):
    """Fresh host facts; the caller owns I/O and remaining-budget enforcement."""

    def saved_runs(self, project: str) -> tuple[str, Iterable[pb.SavedRun]]:
        """The project's root and one lazy scan of its retained runs, in discovery order."""
        ...

    def run_digests(self, request: pb.ListRunDigestsRequest) -> pb.ListRunDigestsResponse:
        """What each requested run's journal says at a glance."""
        ...

    def usage_summary(self, request: pb.UsageSummaryRequest) -> pb.UsageSummaryResponse:
        """What the host's runs and its people's CLI use cost, in the buckets the request cuts."""
        ...

    def usage_rates_refresh(
        self, request: pb.UsageRatesRefreshRequest
    ) -> pb.UsageRatesRefreshResponse:
        """Fetch the price list if one is due; how current the host's rates are after."""
        ...

    def usage_limits(self, request: pb.UsageLimitsRequest) -> pb.UsageLimitsResponse:
        """What each provider's plan limits are, from its CLI or the host's last good reading."""
        ...

    def tasks(self, project: str) -> pb.TaskList:
        """The canonical project's task catalog."""
        ...

    def logins(self) -> list[pb.AgentLogin]:
        """Each vetted harness's current login facts, never its secrets."""
        ...

    def harnesses(self) -> list[pb.HarnessOption]:
        """Vetted harnesses in the agent library's order, from its static catalog.

        The answer names no installed provider: telling which CLIs a machine has takes a
        shell probe, and the machine description already reports them.
        """
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
