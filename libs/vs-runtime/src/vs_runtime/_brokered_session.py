"""A run environment session that also owns a host-side broker."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vs_runtime._run_environment import RunEnvironmentSession, RunEnvironmentView
    from vs_sandbox.api import CommandRunner


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
        # Reverse construction order: the brokers were started first, so they
        # close last. The editor stops first, which ends every connection it held
        # and cancels the jobs those connections asked for. Every close runs even
        # when an earlier one fails.
        with ExitStack() as stack:
            for broker in self.brokers:
                stack.callback(broker.close)
            stack.callback(self.delegate.close)
