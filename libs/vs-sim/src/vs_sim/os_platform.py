"""The operating-system interfaces a process uses, as one value its entrypoint builds once."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sim.blocking import ThreadBlockingRunner
from vs_sim.clock import SystemClock
from vs_sim.concurrency import OsThreads
from vs_sim.network import UnixNetwork
from vs_sim.processes import OsProcesses
from vs_sim.signals import PidfdProcessSignaller

if TYPE_CHECKING:
    from vs_sim.blocking import BlockingRunner
    from vs_sim.clock import SleepingClock
    from vs_sim.concurrency import Threads
    from vs_sim.network import Network
    from vs_sim.processes import Processes
    from vs_sim.signals import ProcessSignaller, WriterProcessSignaller


@dataclass(frozen=True)
class Platform:
    """Time, threads, processes, sockets and signals: real at an entrypoint, simulated in a test.

    Only a component that uses more than three of these takes a ``Platform``. Every other
    component takes the interfaces it uses as arguments of their own.
    """

    clock: SleepingClock
    threads: Threads
    blocking: BlockingRunner
    processes: Processes
    network: Network
    signaller: ProcessSignaller
    writer_signaller: WriterProcessSignaller | None = None

    @classmethod
    def real(cls) -> Platform:
        """The host's own: the wall clock, OS threads and processes, Unix sockets, pidfds."""
        signaller = PidfdProcessSignaller()
        return cls(
            clock=SystemClock(),
            threads=OsThreads(),
            blocking=ThreadBlockingRunner(),
            processes=OsProcesses(),
            network=UnixNetwork(),
            signaller=signaller,
            writer_signaller=signaller,
        )
