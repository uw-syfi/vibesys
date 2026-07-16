"""Guarded process-local bridge between TUI runtime and RunContext."""

from __future__ import annotations

import threading

from vibesys.server.supervisor import RunSupervisor


class SupervisorRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: RunSupervisor | None = None

    def activate(self, supervisor: RunSupervisor) -> None:
        with self._lock:
            if self._active is not None:
                raise RuntimeError("A TUI-supervised run is already active in this process")
            self._active = supervisor

    def get(self) -> RunSupervisor | None:
        with self._lock:
            return self._active

    def deactivate(self, supervisor: RunSupervisor) -> None:
        with self._lock:
            if self._active is supervisor:
                self._active = None


REGISTRY = SupervisorRegistry()


def active_supervisor() -> RunSupervisor | None:
    return REGISTRY.get()
