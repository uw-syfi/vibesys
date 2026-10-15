"""``follow`` copies a growing source into a sink on the injected timeline."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_logs.api import FollowEnd, follow
from vs_sim.api import ByteSource, Capture
from vs_sim.api.testing import FakeByteSource, SimThreads


class _NotYetCreated:
    """A source that does not exist until the test appends to it, then is ``inner``."""

    def __init__(self, inner: FakeByteSource) -> None:
        self.inner = inner
        self.exists = False

    def size(self) -> int:
        if not self.exists:
            raise FileNotFoundError("job.log")
        return self.inner.size()

    def read(self, offset: int, max_bytes: int) -> bytes:
        if not self.exists:
            raise FileNotFoundError("job.log")
        return self.inner.read(offset, max_bytes)


@given(pieces=st.lists(st.binary(max_size=200_000), max_size=6))
def test_every_byte_appended_before_stop_is_copied_once(pieces: list[bytes]) -> None:
    threads = SimThreads()
    source = _NotYetCreated(FakeByteSource())
    pending = list(pieces)
    sink = Capture(limit=10_000_000)

    def stop() -> bool:
        if not pending:
            return True
        source.exists = True
        source.inner.append(pending.pop(0))
        return False

    checked: ByteSource = source
    result = threads.run(lambda: follow(checked, sink, threads=threads, stop=stop))

    assert result.end is FollowEnd.STOPPED
    assert sink.stdout == b"".join(pieces)
    assert result.copied == len(sink.stdout)


def test_a_source_that_shrinks_ends_the_follow() -> None:
    threads = SimThreads()
    source = FakeByteSource(b"first log\n")
    sink = Capture(limit=1000)

    def stop() -> bool:
        if sink.stdout:
            source.truncate(0)
        return False

    result = threads.run(lambda: follow(source, sink, threads=threads, stop=stop))

    assert result.end is FollowEnd.CHANGED
    assert sink.stdout == b"first log\n"
