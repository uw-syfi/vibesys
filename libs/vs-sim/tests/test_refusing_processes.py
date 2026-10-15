"""A refusing Processes fails any test that starts a process, naming the command."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import ProcessSpec
from vs_sim.api.testing import NO_PROCESSES, ProcessStartRefusedError, refused_starts


@given(
    argv=st.lists(st.text(alphabet="abc-_/.", min_size=1, max_size=6), min_size=1, max_size=4),
    timeout=st.none() | st.floats(min_value=0.1, max_value=60),
)
def test_every_run_is_refused_and_names_its_command(argv: list[str], timeout: float | None) -> None:
    spec = ProcessSpec(argv=tuple(argv))
    with pytest.raises(ProcessStartRefusedError, match=" ".join(argv)):
        NO_PROCESSES.run(spec, timeout=timeout)
    assert refused_starts() == (f"this test runs no process, but ran {' '.join(argv)}",)
    assert refused_starts() == ()
