"""A run environment session that also owns a host-side broker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_runtime._run_environment import RunEnvironmentSession, RunEnvironmentView
    from vs_sandbox.api import CommandRunner
    from vs_sandbox.api.slurm import HostCommandBroker, SlurmProcessBroker


@dataclass(slots=True)
class BrokeredRunEnvironmentSession:
    """Own an agent session and the host-side Slurm broker it reaches."""

    delegate: RunEnvironmentSession
    broker: SlurmProcessBroker | HostCommandBroker
    _closed: bool = False

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

    def __enter__(self) -> BrokeredRunEnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Reverse construction order: the broker was started first, so it closes
        # last. The editor stops first, which ends every connection it held and
        # cancels the jobs those connections asked for.
        try:
            self.delegate.close()
        finally:
            self.broker.close()
