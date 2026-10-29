"""Every timestamp the server and its stop client write comes from the clock they were given."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from tests.server.support import CONTROL_KEY, REQUEST_TIME, build_server_parts, operator_dialer

from server.api.protocol import CommandAck, Response, SnapshotQuery, StopCommand
from server.instances import ControlSocketStopRequester
from vibesys.api import ControlAction
from vs_sim.api import utc_datetime
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads

# Seconds since the epoch, from 2001 to 2100: inside the range a datetime holds.
_EPOCH_SECONDS = st.floats(min_value=1e9, max_value=4.1e9, allow_nan=False)


@given(elapsed=_EPOCH_SECONDS)
def test_a_response_carries_the_time_its_api_clock_reads(elapsed: float) -> None:
    parts = build_server_parts()
    clock = parts.clock
    clock.advance(elapsed)

    answered = parts.api.execute(SnapshotQuery(timestamp=REQUEST_TIME))

    assert answered.timestamp == utc_datetime(clock)


@given(start=_EPOCH_SECONDS, frame=st.sampled_from([b"not json", b"[]", b'{"type": "nope"}']))
def test_a_rejected_frame_is_stamped_by_the_api_clock(start: float, frame: bytes) -> None:
    parts = build_server_parts()
    clock = parts.clock
    clock.advance(start)
    api = parts.api

    rejected = api.decode_request(frame)

    assert isinstance(rejected, Response)
    assert (rejected.ok, rejected.timestamp) == (False, utc_datetime(clock))


@given(start=_EPOCH_SECONDS)
def test_a_failed_request_is_stamped_by_the_api_clock(start: float) -> None:
    parts = build_server_parts()
    clock = parts.clock
    clock.advance(start)
    api = parts.api

    failed = api.failure("r", "c", RuntimeError("boom"))

    assert (failed.ok, failed.client_id, failed.timestamp) == (False, "c", utc_datetime(clock))


@given(start=_EPOCH_SECONDS)
def test_the_stop_request_carries_the_time_its_clock_reads(start: float) -> None:
    clock = ManualClock(start)
    threads = SimThreads()
    network = SimNetwork(threads)
    listener = network.listen("/control")
    sent: list[StopCommand] = []

    def serve() -> None:
        connection = listener.accept(HANG_GUARD_S)
        hello = b""
        while not hello.endswith(b"\n"):
            hello += connection.recv(1, HANG_GUARD_S)
        assert CONTROL_KEY.admits(hello), hello
        line = b""
        while not line.endswith(b"\n"):
            line += connection.recv(1, HANG_GUARD_S)
        request = StopCommand.model_validate_json(line)
        sent.append(request)
        reply = Response(
            timestamp=utc_datetime(clock),
            request_id=request.request_id,
            ack=CommandAck(action=ControlAction.PAUSE_NOW, status="pending"),
        )
        connection.send(reply.model_dump_json().encode() + b"\n")
        connection.close()

    def scenario() -> bool:
        threads.spawn(serve, name="server", daemon=True)
        return ControlSocketStopRequester(
            operator_dialer(network), clock=clock, deadline_clock=threads
        ).request_stop("/control")

    assert threads.run(scenario) is True
    assert [request.timestamp for request in sent] == [utc_datetime(clock)]
