"""``WakeupFdSignalRelay`` against real signals: the same contract the in-memory Fake passes.

The unit tests in ``libs/vs-sandbox/tests/test_signal_relay.py`` cover the Fake. What only
the kernel shows is here: a signal sent to this process reaches the callback on the relay
thread, and the handlers and wakeup descriptor are restored on exit. Each case runs in a
forked child because signal handlers belong to the whole process.
"""

from __future__ import annotations

import os
import signal

from vs_sandbox.api import WakeupFdSignalRelay
from vs_sandbox.api.testing import RelayUnderTest, SignalRelayContract
from vs_sim.api import OsThreads
from vs_sim.api.testing import run_in_child, wait_or_fail


class TestWakeupFdSignalRelay(SignalRelayContract):
    def subject(self) -> RelayUnderTest:
        return RelayUnderTest(
            relay=WakeupFdSignalRelay(),
            numbers=(signal.SIGUSR1, signal.SIGUSR2),
            deliver=lambda number: os.kill(os.getpid(), number),
            threads=OsThreads(),
            run=run_in_child,
            wait=wait_or_fail,
        )
