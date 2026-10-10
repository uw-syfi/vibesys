"""The pytest plugin runs async tests deterministically, prints seeds and checks determinism.

Each case writes a small test suite and runs it in a fresh pytest process with the plugin.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import threading
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import (
    EventTrace,
    Sim,
    UnknownWorldError,
    VirtualClock,
    VirtualDeadlockError,
    WorldRegistry,
    run_in_child,
    run_virtual,
)

pytest_plugins = ["pytester"]

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "pytest_plugin"


@pytest.fixture
def suite(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A scratch project that loads the plugin, as the repository's root conftest does."""
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(PLUGIN_DIR), os.environ.get("PYTHONPATH")]))
    )
    pytester.makeini("[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider\n")
    return pytester


def test_async_tests_run_on_the_virtual_loop_without_waiting(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import asyncio

        async def test_an_hour_of_sleep_costs_nothing(sim):
            await sim.clock.sleep(3600)
            assert sim.clock.now() == 3601.0

        async def test_no_fixture_needed():
            await asyncio.sleep(10**6)

        def test_sync_tests_use_sim_run(sim):
            async def main():
                await sim.clock.sleep(5)
                return sim.clock.now()
            assert sim.run(main()) == 6.0
        """
    )
    suite.runpytest_subprocess().assert_outcomes(passed=3)


def test_a_test_that_waits_on_nothing_fails_at_once(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import asyncio

        async def test_waits_on_nothing():
            await asyncio.Event().wait()
        """
    )
    result = suite.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*VirtualDeadlockError*no timer is scheduled*"])


def test_real_system_tiers_are_left_alone(suite: pytest.Pytester) -> None:
    suite.mkdir("tests")
    tier = suite.mkdir("tests/e2e")
    (tier / "test_real.py").write_text("async def test_real():\n    pass\n", encoding="utf-8")
    result = suite.runpytest_subprocess("tests/e2e")
    assert result.parseoutcomes().get("passed", 0) == 0


def test_a_test_cannot_use_both_pytest_asyncio_and_the_sim_fixture(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import pytest

        @pytest.mark.asyncio
        async def test_both(sim):
            pass
        """
    )
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*drop the asyncio marker*"])


def test_a_failure_prints_its_seed_and_the_option_that_replays_it(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        async def test_fails(sim):
            assert sim.seed < 0, "sim.seed=%d" % sim.seed
        """
    )
    first = suite.runpytest_subprocess()
    first.assert_outcomes(failed=1)
    printed = re.search(r"replay with --sim-seed=(\d+)", first.stdout.str())
    assert printed is not None
    seed = int(printed.group(1))
    assert f"sim.seed={seed}" in first.stdout.str()
    replay = suite.runpytest_subprocess(f"--sim-seed={seed}")
    assert f"sim.seed={seed}" in replay.stdout.str()
    other = suite.runpytest_subprocess("--sim-seed=7")
    assert "sim.seed=7" in other.stdout.str()
    assert "replay with --sim-seed=7" in other.stdout.str()


def test_without_an_override_a_tests_seed_is_the_same_every_run(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        async def test_seed(sim):
            print("SEED", sim.seed, sim.random("x").randint(0, 10**9))
        """
    )
    seen = {_seed_line(suite.runpytest_subprocess("-s").stdout.str()) for _ in range(2)}
    assert len(seen) == 1


def _seed_line(output: str) -> str:
    found = re.search(r"SEED \d+ \d+", output)
    assert found is not None, output
    return found.group(0)


def test_the_determinism_check_passes_a_deterministic_test(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import asyncio

        async def test_deterministic(sim):
            async def worker(seconds):
                await sim.clock.sleep(seconds)
            await asyncio.gather(*(worker(sim.random("w").randint(1, 9)) for _ in range(3)))
        """
    )
    suite.runpytest_subprocess("--sim-determinism-check").assert_outcomes(passed=1)


def test_the_determinism_check_catches_a_test_that_depends_on_something_outside_the_clock(
    suite: pytest.Pytester,
) -> None:
    suite.makepyfile(
        """
        import itertools

        outside = itertools.count(1)

        async def test_follows_the_outside_world(sim):
            await sim.clock.sleep(next(outside))
        """
    )
    result = suite.runpytest_subprocess("--sim-determinism-check")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*not deterministic*advance*"])
    suite.runpytest_subprocess().assert_outcomes(passed=1)


def test_the_determinism_check_does_not_hide_an_ordinary_failure(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        async def test_fails(sim):
            assert False, "plain failure"
        """
    )
    result = suite.runpytest_subprocess("--sim-determinism-check")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*plain failure*"])


def test_worlds_registered_by_a_project_are_built_once_per_test(suite: pytest.Pytester) -> None:
    suite.makeconftest(
        """
        from vs_sim.api.testing import WORLDS

        class Cluster:
            def __init__(self, clock):
                self.clock = clock

        WORLDS.register("cluster", lambda sim: Cluster(sim.clock))
        """
    )
    suite.makepyfile(
        """
        import pytest
        from vs_sim.api.testing import UnknownWorldError

        async def test_world(sim):
            cluster = sim.world("cluster")
            assert cluster is sim.world("cluster")
            assert cluster.clock is sim.clock

        def test_unknown(sim):
            with pytest.raises(UnknownWorldError, match="registered: cluster"):
                sim.world("nothing")
        """
    )
    suite.runpytest_subprocess().assert_outcomes(passed=2)


def test_the_header_names_the_seed_policy(suite: pytest.Pytester) -> None:
    suite.makepyfile("def test_x():\n    pass\n")
    assert "vs-sim: per-test seeds" in suite.runpytest_subprocess().stdout.str()
    header = suite.runpytest_subprocess("--sim-seed=3", "--sim-determinism-check").stdout.str()
    assert "vs-sim: seed 3 for every sim test, determinism check on" in header


def _alarm_during_a_worker_wait() -> str:
    """What ``timeout_method = "signal"`` does: SIGALRM raises from its handler in the main thread."""
    release = threading.Event()

    def on_alarm(_number: int, _frame: object) -> None:
        message = "the test bound"
        raise TimeoutError(message)

    signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, 0.05)

    def hold() -> None:
        release.wait(60)

    async def main() -> None:
        try:
            await asyncio.to_thread(hold)
        finally:
            release.set()

    try:
        run_virtual(VirtualClock(), main())
    except TimeoutError as error:
        return str(error)
    return "no timeout"


def test_the_signal_timeout_method_interrupts_a_virtual_run() -> None:
    """The hard bound still ends a run stuck on a worker thread; the loop closes cleanly."""
    assert run_in_child(_alarm_during_a_worker_wait) == "the test bound"


@given(st.integers(min_value=0, max_value=2**32), st.text(max_size=8), st.text(max_size=8))
def test_a_sims_random_streams_depend_only_on_seed_and_label(
    seed: int, label: str, other: str
) -> None:
    first, second = Sim(seed=seed), Sim(seed=seed)
    assert [first.random(label).random() for _ in range(3)] == [
        second.random(label).random() for _ in range(3)
    ]
    first.random(other).random()  # drawing from another label moves nothing
    assert first.random(label).random() == second.random(label).random()


def test_a_world_name_has_one_factory() -> None:
    registry = WorldRegistry()
    registry.register("a", lambda _sim: object())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("a", lambda _sim: object())
    with pytest.raises(UnknownWorldError, match="registered: a"):
        Sim(seed=0, worlds=registry).world("b")


def test_a_sim_runs_coroutines_on_its_own_clock_and_records_its_trace() -> None:
    trace = EventTrace()
    sim = Sim(seed=1, trace=trace)

    async def main() -> float:
        await sim.clock.sleep(4.0)
        return sim.clock.now()

    assert sim.run(main()) == 5.0
    assert ("advance", "1.0", "5.0") in trace.events
    assert sim.gate().is_open is False


def test_a_sim_gate_deadlocks_instead_of_hanging() -> None:
    sim = Sim(seed=1)
    gate = sim.gate()

    async def main() -> None:
        await gate.wait()

    with pytest.raises(VirtualDeadlockError):
        sim.run(main())
