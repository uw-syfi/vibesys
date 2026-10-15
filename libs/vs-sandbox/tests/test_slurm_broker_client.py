"""The Slurm broker client against a broker that does not answer by its protocol.

``BrokeredProcesses`` is the ``Processes`` behind ``vs_slurm``'s transport, whose runner
maps ``FileNotFoundError`` and a timed-out run and whose callers catch ``OSError``. Against
a scripted broker on a ``SimNetwork``, in virtual time, every reply ends in a completed
process, a timed-out run, or an ``OSError``: a refusal, or a typed ``BrokerReplyError`` for
a reply outside the protocol or a hang-up.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import BrokeredProcesses, BrokerReplyError
from vs_sim.api import ProcessOutcome, ProcessSpec, ProcessTimedOut
from vs_sim.api.testing import (
    HANG_GUARD_S,
    SimNetwork,
    SimThreads,
    VirtualClock,
    hostile_json_frames,
)
from vs_slurm.api import MAX_TRANSPORT_STREAM_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Connection

ADDRESS = "slurm-broker.sock"
TIMEOUT_S = 30.0
_RAN = json.dumps({"ok": True, "result": {"returncode": 0, "stdout": "o", "stderr": "e"}})
_REFUSED = json.dumps({"ok": False, "error": "unauthorized destination"})
_WRONG_TYPES = (
    ("ok-without-result", b'{"ok":true}'),
    ("ok-is-a-string", b'{"ok":"true","error":"x"}'),
    ("ok-is-a-number", b'{"ok":1,"error":"x"}'),
    ("refusal-without-error", b'{"ok":false}'),
    ("result-is-a-list", b'{"ok":true,"result":[]}'),
    ("returncode-is-a-string", _RAN.replace('"returncode":0', '"returncode":"0"').encode()),
    ("returncode-is-a-bool", _RAN.replace('"returncode":0', '"returncode":true').encode()),
    ("stdout-is-null", _RAN.replace('"stdout":"o"', '"stdout":null').encode()),
    ("an-extra-key", _RAN.replace("{", '{"extra":1,', 1).encode()),
    ("a-list-naming-a-key", b'["ok"]'),
)
_FRAMES = (*hostile_json_frames((_RAN.encode(), _REFUSED.encode())), *_WRONG_TYPES)
_FRAME_BOUND = 16 * 1024 * 1024
_Completed = ProcessOutcome
_TimedOut = ProcessTimedOut

type Broker = Callable[[Connection], None]
"""What the scripted broker does once it has read the request line."""


def _read_request(connection: Connection) -> bytes:
    received = b""
    while not received.endswith(b"\n"):
        chunk = connection.recv(1 << 16, HANG_GUARD_S)
        if not chunk:
            break
        received += chunk
    return received


def _call(
    broker: Broker, *, seed: int | None, buffer: int | None = None, serve: bool = True
) -> tuple[_Completed | OSError | _TimedOut, float, bytes]:
    """What the client returns or raises against ``broker``, the virtual time it took,
    and the request line the broker read."""
    clock = VirtualClock()
    threads = SimThreads(clock, schedule_seed=seed)
    network = SimNetwork(threads, buffer_bytes=buffer)
    request: list[bytes] = [b""]

    def program() -> _Completed | OSError | _TimedOut:
        listener = network.listen(ADDRESS) if serve else None

        def respond() -> None:
            assert listener is not None
            connection = listener.accept(HANG_GUARD_S)
            try:
                request[0] = _read_request(connection)
                broker(connection)
            finally:
                connection.close()

        worker = threads.spawn(respond, name="broker") if serve else None
        try:
            run = BrokeredProcesses(Path(ADDRESS), "token", network=network).run(
                ProcessSpec(("ssh", "--", "cluster", "hostname")), timeout=TIMEOUT_S
            )
        except OSError as error:
            # Anything else escapes and fails the test.
            return error
        else:
            assert isinstance(run, (_Completed, _TimedOut)), run
            return run
        finally:
            if worker is not None:
                worker.join(HANG_GUARD_S)
            if listener is not None:
                listener.close()

    start = clock.now()
    outcome = threads.run(program)
    return outcome, clock.now() - start, request[0]


def _replying(frame: bytes) -> Broker:
    def broker(connection: Connection) -> None:
        with suppress(OSError):
            connection.send(frame + b"\n", HANG_GUARD_S)

    return broker


@pytest.mark.parametrize(
    ("index", "name", "frame"),
    [(index, name, frame) for index, (name, frame) in enumerate(_FRAMES)],
    ids=[name for name, _ in _FRAMES],
)
def test_every_reply_is_a_result_a_refusal_or_a_typed_error(
    index: int, name: str, frame: bytes
) -> None:
    outcome, _, _ = _call(_replying(frame), seed=None if index % 2 else index)

    if isinstance(outcome, _Completed):
        assert frame == _RAN.encode(), f"{name}: only the well-formed result runs"
    elif isinstance(outcome, PermissionError):
        assert frame == _REFUSED.encode(), f"{name}: only the well-formed refusal refuses"
        assert str(outcome) == "unauthorized destination"
    else:
        assert isinstance(outcome, BrokerReplyError), f"{name}: {outcome!r}"


def test_a_well_formed_result_is_the_completed_process() -> None:
    outcome, _, request = _call(_replying(_RAN.encode()), seed=3)

    assert isinstance(outcome, _Completed)
    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (0, b"o", b"e")
    assert json.loads(request) == {
        "token": "token",
        "argv": ["ssh", "--", "cluster", "hostname"],
        "stdin": None,
        "timeout": TIMEOUT_S,
        "max_output_bytes": MAX_TRANSPORT_STREAM_BYTES,
    }


@pytest.mark.parametrize("sent", [b"", b'{"ok":true,"res'])
def test_a_broker_that_hangs_up_without_a_whole_reply_is_a_reply_error(sent: bytes) -> None:
    outcome, _, _ = _call(lambda connection: connection.send(sent, HANG_GUARD_S), seed=5)

    assert isinstance(outcome, BrokerReplyError)
    assert "without a reply" in str(outcome)


@pytest.mark.parametrize("seed", [None, 11])
def test_a_silent_broker_is_a_timeout_after_the_call_timeout(seed: int | None) -> None:
    def silent(connection: Connection) -> None:
        # Wait for the client to give up and hang up; the guard only bounds a hang.
        with suppress(OSError, TimeoutError):
            connection.recv(1, HANG_GUARD_S)

    outcome, took, _ = _call(silent, seed=seed)

    assert isinstance(outcome, _TimedOut)
    assert outcome.timeout == TIMEOUT_S
    assert took == pytest.approx(TIMEOUT_S)


def test_a_broker_nobody_serves_is_file_not_found() -> None:
    outcome, _, _ = _call(_replying(b""), seed=None, serve=False)

    assert isinstance(outcome, FileNotFoundError)


@pytest.mark.parametrize("seed", [None, 13])
def test_a_reply_that_never_ends_is_cut_off_at_the_frame_bound(seed: int | None) -> None:
    sent = [0]
    chunk = b"x" * (1 << 16)

    def endless(connection: Connection) -> None:
        # A bounded buffer makes each send wait for the client to read, so the broker
        # stops once the client hangs up; the cap only guards a client that never would.
        with suppress(OSError, TimeoutError):
            while sent[0] < 4 * _FRAME_BOUND:
                connection.send(chunk, HANG_GUARD_S)
                sent[0] += len(chunk)

    outcome, _, _ = _call(endless, seed=seed, buffer=1 << 20)

    assert isinstance(outcome, BrokerReplyError)
    assert "longer than" in str(outcome)
    assert sent[0] < _FRAME_BOUND + 4 * (1 << 20), "the client stopped reading at the bound"
