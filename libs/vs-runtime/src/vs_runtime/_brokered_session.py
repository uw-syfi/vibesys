"""A run environment session that also owns a host-side broker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from vs_sandbox.api import CaptureExecutionIdentity, CaptureExecutionKind
from vs_sim.api import CloseOnce, ReleaseStack

if TYPE_CHECKING:
    from vs_runtime._run_environment import RunEnvironmentSession, RunEnvironmentView
    from vs_sandbox.api import CaptureCommandRunner, CommandRunner


class HostBroker(Protocol):
    """A host-side service, such as a command or transport broker, that a run owns."""

    def close(self) -> None:
        """Stop serving and release the broker's socket."""
        ...


@dataclass(slots=True)
class BrokeredRunEnvironmentSession:
    """Own an agent session and the host-side brokers it reaches.

    *brokers* are in construction order.
    """

    delegate: RunEnvironmentSession
    brokers: tuple[HostBroker, ...]
    capture_override: CaptureCommandRunner | None = None
    _ownership: ReleaseStack = field(init=False, repr=False)
    _close: CloseOnce = field(default_factory=CloseOnce, init=False, repr=False)

    def __post_init__(self) -> None:
        """Record every owned close once, in construction order."""
        self._ownership = ReleaseStack()
        for broker in self.brokers:
            self._ownership.callback(broker.close)
        self._ownership.callback(self.delegate.close)

    @property
    def sandbox(self) -> CommandRunner:
        return self.delegate.sandbox

    @sandbox.setter
    def sandbox(self, value: CommandRunner) -> None:
        self.delegate.sandbox = value

    @property
    def view(self) -> RunEnvironmentView:
        return self.delegate.view

    @view.setter
    def view(self, value: RunEnvironmentView) -> None:
        self.delegate.view = value

    @property
    def capture_commands(self) -> CaptureCommandRunner:
        """Use the role-specific override, otherwise the delegate's runner."""
        if self.capture_override is not None:
            return self.capture_override
        return self.delegate.capture_commands

    @property
    def capture_execution(self) -> CaptureExecutionIdentity | None:
        """Return a durable identity only for the independently recoverable runner."""
        if self.capture_override is None:
            return None
        return CaptureExecutionIdentity(
            kind=CaptureExecutionKind.SLURM_GPU,
            execution_id=self.capture_override.id,
        )

    def __enter__(self) -> BrokeredRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Release every owner; retry only the steps whose earlier close failed."""
        # Reverse construction order: the brokers were started first, so they
        # close last. The editor stops first, which ends every connection it held
        # and cancels the jobs those connections asked for. Every remaining close
        # runs even when an earlier one fails.
        self._close.run(self._ownership.close)
