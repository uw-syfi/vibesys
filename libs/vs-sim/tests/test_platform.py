"""A simulated ``Platform`` keeps every interface on the simulation's own timeline."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import ProcessOutcome, ProcessSpec
from vs_sim.api.testing import FakeProcesses, FakeProcessSignaller, ProcessScript, Sim


@given(seconds=st.floats(min_value=0, max_value=3600), seed=st.integers(0, 2**16))
def test_blocking_work_on_a_simulated_platform_sleeps_in_virtual_time(
    seconds: float, seed: int
) -> None:
    sim = Sim(seed=seed)
    threads = sim.threads()
    processes = FakeProcesses(lambda _spec: ProcessScript(returncode=3))
    platform = sim.platform(threads, processes=processes, signaller=FakeProcessSignaller(set()))
    start = sim.clock.now()

    def work() -> ProcessOutcome:
        platform.threads.sleep(seconds)
        run = platform.processes.run(ProcessSpec(("true",)), timeout=None)
        assert isinstance(run, ProcessOutcome)
        return run

    async def main() -> ProcessOutcome:
        return await platform.blocking.run(work)

    assert sim.run(main(), threads=threads) == ProcessOutcome(3, b"", b"")
    assert platform.clock.now() == start + seconds
