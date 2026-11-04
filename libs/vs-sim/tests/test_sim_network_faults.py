"""SimNetwork's link faults: each one ends in a defined state on both ends, never a hang.

Every case runs one echo exchange with no timeout on any read or write: the dialer sends
numbered lines, the acceptor echoes each, and the dialer closes after the last echo. A fault
that left either end blocked for ever would trip the hang guard on joining it.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    HANG_GUARD_S,
    End,
    LinkFault,
    LinkFaultKind,
    SimNetwork,
    SimThreads,
    VirtualClock,
)

if TYPE_CHECKING:
    from vs_sim.api import Connection

_LINES = 4
_SENDS = 2 * _LINES
"""The exchange's sends, counting both ends: a line and its echo each."""


@dataclass
class _Side:
    """What one end saw."""

    sent: dict[bytes, float] = field(default_factory=dict)
    """Each line sent, and when."""
    arrived: dict[bytes, float] = field(default_factory=dict)
    """Each whole line received, and when."""
    ended: BaseException | None = None
    eof: bool = False
    at: float = 0.0


def _receive(
    threads: SimThreads, connection: Connection, side: _Side, buffer: bytearray
) -> bytes | None:
    """The next whole line, recorded on ``side``; ``None`` at the end of the stream."""
    while b"\n" not in buffer:
        chunk = connection.recv(64)
        if not chunk:
            side.eof = True
            return None
        buffer += chunk
    line, _, rest = bytes(buffer).partition(b"\n")
    buffer[:] = rest
    side.arrived[line] = threads.now()
    return line


def _exchange(threads: SimThreads, network: SimNetwork) -> tuple[_Side, _Side]:
    dialer, acceptor = _Side(), _Side()
    listener = network.listen("echo")

    def serve() -> None:
        connection = listener.accept(HANG_GUARD_S)
        buffer = bytearray()
        try:
            while (line := _receive(threads, connection, acceptor, buffer)) is not None:
                echo = b"echo " + line
                acceptor.sent[echo] = threads.now()
                connection.send(echo + b"\n")
        except OSError as error:
            acceptor.ended = error
        acceptor.at = threads.now()
        connection.close()

    def dial() -> None:
        connection = network.connect("echo")
        buffer = bytearray()
        try:
            for index in range(_LINES):
                line = b"line %d" % index
                dialer.sent[line] = threads.now()
                connection.send(line + b"\n")
                threads.sleep(0.25)
                if _receive(threads, connection, dialer, buffer) is None:
                    break
        except OSError as error:
            dialer.ended = error
        dialer.at = threads.now()
        connection.close()

    server = threads.spawn(serve, name="acceptor")
    client = threads.spawn(dial, name="dialer")
    for worker in (client, server):
        worker.join(HANG_GUARD_S)
        assert not worker.is_alive(), f"the {worker.name} hung"
    listener.close()
    return dialer, acceptor


def _run(
    fault: LinkFault, schedule: int, buffer_bytes: int | None = None
) -> tuple[SimNetwork, _Side, _Side]:
    threads = SimThreads(VirtualClock(), schedule_seed=schedule)
    network = SimNetwork(threads, buffer_bytes=buffer_bytes, faults=(fault,))
    dialer, acceptor = threads.run(lambda: _exchange(threads, network))
    return network, dialer, acceptor


FAULTS = st.builds(
    LinkFault,
    kind=st.sampled_from(LinkFaultKind),
    end=st.sampled_from(End),
    before_send=st.integers(1, _SENDS),
    seconds=st.floats(0.5, 5.0),
)


BUFFERS = st.sampled_from([None, 8])
"""Unbounded, or smaller than one echo, so a send waits for the reader."""


@given(fault=FAULTS, schedule=st.integers(0, 2**16), buffer_bytes=BUFFERS)
def test_every_link_fault_ends_both_ends_in_its_defined_state(
    fault: LinkFault, schedule: int, buffer_bytes: int | None
) -> None:
    network, dialer, acceptor = _run(fault, schedule, buffer_bytes)
    [fired] = network.fired
    assert fired.fault == fault
    sides = {End.DIALER: dialer, End.ACCEPTOR: acceptor}
    toward = sides[fault.end]
    sender = sides[End.ACCEPTOR if fault.end is End.DIALER else End.DIALER]
    intact = all(line in dialer.arrived for line in (b"echo line %d" % i for i in range(_LINES)))
    if fault.kind is LinkFaultKind.DROP:
        assert isinstance(toward.ended, ConnectionAbortedError), toward
        assert sender.ended is None or isinstance(sender.ended, BrokenPipeError), sender
        assert sender.eof or isinstance(sender.ended, BrokenPipeError), sender
    elif fault.kind is LinkFaultKind.RESET:
        assert isinstance(dialer.ended, ConnectionResetError), dialer
        assert isinstance(acceptor.ended, ConnectionResetError), acceptor
    elif fault.kind is LinkFaultKind.BLACKHOLE:
        # The end the bytes went toward waits for what vanished, so the connection breaks
        # for it exactly when the bound runs out; its peer finished before, or broke too.
        breaks_at = fired.at + fault.seconds
        for side in (toward, sender):
            if side is sender and side.ended is None:
                assert side.at <= breaks_at, (side, fired)
                continue
            assert isinstance(side.ended, ConnectionAbortedError), side
            assert side.ended.errno == errno.ETIMEDOUT, side
            assert side.at == breaks_at, (side, fired)
    else:
        assert dialer.ended is None, dialer
        assert acceptor.ended is None, acceptor
        assert intact, dialer
        assert acceptor.eof, acceptor
        for line, sent_at in sender.sent.items():
            if _ordinal(line) < fault.before_send:
                continue
            arrived_at = toward.arrived[line]
            if fault.kind is LinkFaultKind.STALL:
                assert arrived_at >= fired.at + fault.seconds, (line, sent_at, arrived_at)
            else:
                assert arrived_at >= sent_at + fault.seconds, (line, sent_at, arrived_at)


def _ordinal(line: bytes) -> int:
    """Which send on the connection carried ``line``: lines and echoes alternate."""
    index = int(line.rsplit(b" ", 1)[1])
    return 2 * index + (2 if line.startswith(b"echo") else 1)


@given(schedule=st.integers(0, 2**16))
def test_a_fault_on_a_later_connection_or_send_leaves_the_exchange_whole(schedule: int) -> None:
    for fault in (
        LinkFault(LinkFaultKind.RESET, connection=2),
        LinkFault(LinkFaultKind.RESET, before_send=_SENDS + 1),
    ):
        network, dialer, acceptor = _run(fault, schedule)
        assert network.fired == ()
        assert dialer.ended is None
        assert acceptor.ended is None
        assert len(dialer.arrived) == _LINES
