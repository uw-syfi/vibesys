"""The read-only servers a host runs so Chat can read runs that have no live server.

A run server holds its run's chat. A run that ended has none, so the host helper starts the run
server in its read-only mode for it (``vibesys --detach --reopen``), and shares that one server
among every client that asks for the run while it lives. The server registers in a registry of its
own, apart from the one live runs register in: a reader drives nothing, so it is never listed as a
run, never blocks a resume of the run it serves, and is never stopped as one. It ends by itself
once no client holds a connection to it (``READER_IDLE_SECONDS``, enforced by
``server.runtime``), so nothing the helper does, and no helper that dies, leaves one behind.

The decision is the registry's: a run with a reader is served by it, one without starts a reader.
Two helpers on one host that ask at the same moment may each start one; both end by themselves.

Light: the standard library, ``server.host_api``, ``server.registry`` and ``vs_sim.api``.

Public surface: ``Readers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.registry import driving

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.host_api import DetachedLaunchFailure, LiveInstanceRecord
    from server.registry import LiveRegistry
    from vs_sim.api import Lock, Threads


class Readers:
    """The read-only servers of one host, found by the run each serves."""

    def __init__(self, registry: LiveRegistry, threads: Threads) -> None:
        """Find readers in ``registry``, the registry readers register in; ``threads`` makes the lock."""
        self._registry = registry
        self._guard: Lock = threads.lock()

    def serve(
        self, run_id: str, start: Callable[[], LiveInstanceRecord | DetachedLaunchFailure]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        """The reader of ``run_id`` if one is live, else what ``start`` starts.

        ``start`` runs at most once per call and never while another call of this object is
        deciding, so concurrent requests of one helper share the server the first one starts.
        """
        with self._guard:
            live = driving(self._registry.list(), run_id)
            return live if live is not None else start()

    def find(self, instance_id: str) -> LiveInstanceRecord | None:
        """The live reader named ``instance_id`` that a client can still reach, else ``None``."""
        return self._registry.find(instance_id)
