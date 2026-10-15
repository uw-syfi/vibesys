"""The pytest plugin runs async tests deterministically, prints seeds and checks determinism.

Each case writes a small test suite and runs it with the plugin inside this process
(``runpytest_inprocess``): a fresh interpreter per run would cost seconds of start-up each,
and the plugin's one global, the session depth, is put back after every case.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_sim.api.testing import (
    EventTrace,
    Sim,
    UnknownWorldError,
    VirtualClock,
    VirtualDeadlockError,
    WorldRegistry,
    depth,
    explore_seed,
    run_in_child,
    run_virtual,
    using_depth,
)

if TYPE_CHECKING:
    from collections.abc import Generator

pytest_plugins = ["pytester"]

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "pytest_plugin"


@pytest.fixture
def suite(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Generator[pytest.Pytester]:
    """A scratch project that loads the plugin, as the repository's root conftest does."""
    monkeypatch.syspath_prepend(str(PLUGIN_DIR))
    pytester.makeini("[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider -p no:tach\n")
    with using_depth(depth()):  # each inner run's configure sets the depth; this session's survives
        yield pytester


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
    suite.runpytest_inprocess().assert_outcomes(passed=3)


def test_hypothesis_examples_of_an_async_test_each_run_virtually(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        from hypothesis import given, settings, strategies as st
        from vs_sim.api.testing import current_virtual_clock

        @settings(max_examples=5, deadline=None)
        @given(st.floats(min_value=1, max_value=10**6))
        async def test_sleeps_of_any_length(seconds):
            clock = current_virtual_clock()
            before = clock.now()
            await clock.sleep(seconds)
            assert clock.now() - before == __import__("pytest").approx(seconds)
        """
    )
    suite.runpytest_inprocess().assert_outcomes(passed=1)
    suite.runpytest_inprocess("--sim-determinism-check").assert_outcomes(passed=1)


def test_a_test_that_waits_on_nothing_fails_at_once(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import asyncio

        async def test_waits_on_nothing():
            await asyncio.Event().wait()
        """
    )
    result = suite.runpytest_inprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*VirtualDeadlockError*no timer is scheduled*"])


def test_real_system_tiers_are_left_alone(suite: pytest.Pytester) -> None:
    suite.mkdir("tests")
    tier = suite.mkdir("tests/e2e")
    (tier / "test_real.py").write_text("async def test_real():\n    pass\n", encoding="utf-8")
    result = suite.runpytest_inprocess("tests/e2e")
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
    result = suite.runpytest_inprocess("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*drop the asyncio marker*"])


def test_a_failure_prints_its_seed_and_the_option_that_replays_it(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        async def test_fails(sim):
            assert sim.seed < 0, "sim.seed=%d" % sim.seed
        """
    )
    first = suite.runpytest_inprocess()
    first.assert_outcomes(failed=1)
    printed = re.search(r"replay with --sim-seed=(\d+)", first.stdout.str())
    assert printed is not None
    seed = int(printed.group(1))
    assert f"sim.seed={seed}" in first.stdout.str()
    replay = suite.runpytest_inprocess(f"--sim-seed={seed}")
    assert f"sim.seed={seed}" in replay.stdout.str()
    other = suite.runpytest_inprocess("--sim-seed=7")
    assert "sim.seed=7" in other.stdout.str()
    assert "replay with --sim-seed=7" in other.stdout.str()


def test_without_an_override_a_tests_seed_is_the_same_every_run(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        async def test_seed(sim):
            print("SEED", sim.seed, sim.random("x").randint(0, 10**9))
        """
    )
    seen = {_seed_line(suite.runpytest_inprocess("-s").stdout.str()) for _ in range(2)}
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
    suite.runpytest_inprocess("--sim-determinism-check").assert_outcomes(passed=1)


def test_the_determinism_check_preserves_named_tasks(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import asyncio

        async def test_named_task(sim):
            task = asyncio.create_task(sim.clock.sleep(1), name="worker")
            assert task.get_name() == "worker"
            await task
        """
    )
    suite.runpytest_inprocess("--sim-determinism-check").assert_outcomes(passed=1)


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
    result = suite.runpytest_inprocess("--sim-determinism-check")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*not deterministic*advance*"])
    suite.runpytest_inprocess().assert_outcomes(passed=1)


def test_the_determinism_check_does_not_hide_an_ordinary_failure(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        async def test_fails(sim):
            assert False, "plain failure"
        """
    )
    result = suite.runpytest_inprocess("--sim-determinism-check")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*plain failure*"])


def test_worlds_registered_by_a_project_are_built_once_per_test(suite: pytest.Pytester) -> None:
    suite.makeconftest(
        """
        from vs_sim.api.testing import WORLDS

        class Cluster:
            def __init__(self, clock):
                self.clock = clock

        # WORLDS is process-wide, and every in-process run of this test (seed exploration
        # repeats it) imports this conftest again; a project's conftest is imported once.
        if "cluster" not in WORLDS.names:
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
    suite.runpytest_inprocess().assert_outcomes(passed=2)


def test_the_header_names_the_seed_policy(suite: pytest.Pytester) -> None:
    suite.makepyfile("def test_x():\n    pass\n")
    assert "vs-sim: per-test seeds" in suite.runpytest_inprocess().stdout.str()
    header = suite.runpytest_inprocess("--sim-seed=3", "--sim-determinism-check").stdout.str()
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


ORDER_DEPENDENT_SUITE = """
import asyncio

async def test_two_tasks_race_for_a_shared_slot():
    winner = []

    async def claim(name):
        await asyncio.sleep(1)
        winner.append(name)

    await asyncio.gather(claim("a"), claim("b"))
    assert winner == ["a", "b"], "the test assumes a always claims first"
"""


def test_explore_fails_an_order_dependent_test_and_prints_the_replay_options(
    suite: pytest.Pytester,
) -> None:
    suite.makepyfile(ORDER_DEPENDENT_SUITE)
    suite.runpytest_inprocess().assert_outcomes(passed=1)  # the default order hides the bug
    result = suite.runpytest_inprocess("--sim-explore=24")
    result.assert_outcomes(failed=1)
    match = re.search(
        r"replay with --sim-seed=(\d+) --sim-schedule-seed=(\d+)", result.stdout.str()
    )
    assert match is not None
    assert match.group(1) == match.group(2)
    replay = suite.runpytest_inprocess(
        f"--sim-seed={match.group(1)}", f"--sim-schedule-seed={match.group(2)}"
    )
    replay.assert_outcomes(failed=1)


def test_explore_runs_each_test_the_requested_number_of_times(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        """
        import asyncio
        from pathlib import Path

        async def test_sim():
            await asyncio.sleep(1)
            with Path("sim_runs").open("a") as out:
                out.write("x")

        def test_plain():
            with Path("plain_runs").open("a") as out:
                out.write("x")
        """
    )
    result = suite.runpytest_inprocess("--sim-explore=5", "--sim-repeat=3")
    result.assert_outcomes(passed=2)
    assert (suite.path / "sim_runs").read_text() == "x" * 5
    assert (suite.path / "plain_runs").read_text() == "x" * 3
    result.stdout.fnmatch_lines(["vs-sim exploration: 8 of 8 planned runs in *"])


def test_explore_deselects_real_tiers_and_other_tests_unless_repeated(
    suite: pytest.Pytester,
) -> None:
    suite.makeini(
        "[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider -p no:tach\nsim_real_tiers =\n    real\n"
    )
    suite.makepyfile(test_top="def test_plain():\n    pass\n\nasync def test_sim():\n    pass\n")
    (suite.path / "real").mkdir()
    (suite.path / "real" / "test_real.py").write_text("def test_real():\n    pass\n")
    suite.runpytest_inprocess("--sim-explore=2").assert_outcomes(passed=1, deselected=2)
    suite.runpytest_inprocess("--sim-explore=2", "--sim-repeat=2").assert_outcomes(
        passed=2, deselected=1
    )


def test_a_spent_budget_cuts_even_a_tests_first_exploration_run(suite: pytest.Pytester) -> None:
    suite.makepyfile("async def test_sim():\n    pass\n")
    result = suite.runpytest_inprocess(
        "--sim-explore=50", "--sim-explore-budget=0", "--sim-explore-report=explored.jsonl", "-rs"
    )
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(
        [
            "vs-sim exploration: 0 of 50 *",
            "*not explored: the budget or an interrupt ended exploration first*",
        ]
    )
    assert _explored(suite.path / "explored.jsonl") == {
        "test_a_spent_budget_cuts_even_a_tests_first_exploration_run.py::test_sim": (
            0,
            50,
            "not explored",
        )
    }


COUNTING_SUITE = """
from pathlib import Path

def _run(name):
    counter = Path({example!r}) / name
    count = int(counter.read_text()) if counter.exists() else 0
    if name == {interrupter!r} and count == {interrupt_run}:
        raise KeyboardInterrupt
    counter.write_text(str(count + 1))

{tests}
"""


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    tests=st.integers(1, 4),
    data=st.data(),
    interrupt_run=st.integers(0, 3),
    salt=st.text("0123456789abcdef", min_size=1, max_size=8),
)
def test_an_interrupt_leaves_every_test_explored_within_one_run_of_the_others(
    suite: pytest.Pytester, tests: int, data: st.DataObject, interrupt_run: int, salt: str
) -> None:
    """Exploration runs in rounds, so a cut (budget or interrupt) never starves later tests."""
    names = [f"test_{index}" for index in range(tests)]
    interrupter = data.draw(st.sampled_from(names))
    # A new module and counter directory per example: a rewritten module of the same size
    # and second would otherwise load from its stale bytecode cache.
    example = suite.mkdir(f"example_{len(list(suite.path.glob('example_*')))}")
    module = example / "test_counting.py"
    module.write_text(
        COUNTING_SUITE.format(
            example=str(example),
            interrupter=interrupter,
            interrupt_run=interrupt_run,
            tests="\n".join(f"def {name}():\n    _run({name!r})\n" for name in names),
        )
    )
    report = example / "explored.jsonl"
    result = suite.runpytest_inprocess(
        str(module),
        "--sim-explore=1",
        "--sim-repeat=5",
        f"--sim-explore-salt={salt}",
        f"--sim-explore-report={report}",
    )

    explored = {test.rpartition("::")[2]: row for test, row in _explored(report).items()}
    runs = {name: row[0] for name, row in explored.items()}
    counted = {
        name: int((example / name).read_text()) if (example / name).exists() else 0
        for name in names
    }
    assert runs == counted
    assert {row[2] for row in explored.values() if row[0] == 0} <= {"not explored"}
    assert {row[2] for row in explored.values() if row[0] > 0} <= {"passed"}
    assert runs[interrupter] == interrupt_run
    assert set(runs.values()) <= {interrupt_run, interrupt_run + 1}
    ran = sum(1 for count in runs.values() if count)
    result.assert_outcomes(passed=ran, skipped=tests - ran)
    result.stdout.fnmatch_lines(["vs-sim exploration: * stopped by an interrupt)"])


def test_the_salt_picks_the_exploration_seeds(suite: pytest.Pytester) -> None:
    suite.makepyfile(
        test_seeds="""
        from pathlib import Path

        async def test_sim(sim):
            with Path("seeds").open("a") as out:
                out.write(f"{sim.seed}\\n")
        """
    )

    def seeds(*salt: str) -> list[int]:
        (suite.path / "seeds").unlink(missing_ok=True)
        suite.runpytest_inprocess("--sim-explore=3", *salt).assert_outcomes(passed=1)
        return [int(line) for line in (suite.path / "seeds").read_text().split()]

    unsalted = seeds()
    assert unsalted == [explore_seed("test_seeds.py::test_sim", run) for run in range(3)]
    salted = seeds("--sim-explore-salt=abc")
    assert salted == seeds("--sim-explore-salt=abc")
    assert len(set(salted)) == 3
    fixed = 2  # the plugin's FIXED_EXPLORATION_RUNS, a documented part of --sim-explore-salt
    # The first runs repeat under every salt, so a failing seed is retried on the next push;
    # the later runs differ by salt.
    assert salted[:fixed] == unsalted[:fixed] == seeds("--sim-explore-salt=abd")[:fixed]
    assert not set(salted[fixed:]) & set(seeds("--sim-explore-salt=abd"))
    assert not set(salted[fixed:]) & set(unsalted)


def test_a_failure_found_under_one_salt_is_replayed_first_under_the_next(
    suite: pytest.Pytester,
) -> None:
    suite.makepyfile(test_race=ORDER_DEPENDENT_SUITE)
    found = suite.path / "found.jsonl"
    suite.runpytest_inprocess(
        "--sim-explore=24", "--sim-explore-salt=push-1", f"--sim-explore-report={found}"
    ).assert_outcomes(failed=1)
    [row] = [json.loads(line) for line in found.read_text().splitlines()]
    assert row["outcome"] == "failed"
    assert isinstance(row["seed"], int)
    replays = suite.path / "replays.jsonl"
    replays.write_text(json.dumps({"test": row["test"], "seed": row["seed"]}) + "\n")
    again = suite.path / "again.jsonl"

    # One run under a new salt is the replayed seed, so it fails the same way.
    suite.runpytest_inprocess(
        "--sim-explore=1",
        "--sim-explore-salt=push-2",
        f"--sim-explore-replay={replays}",
        f"--sim-explore-report={again}",
    ).assert_outcomes(failed=1)
    [replayed] = [json.loads(line) for line in again.read_text().splitlines()]
    assert (replayed["runs"], replayed["seed"]) == (1, row["seed"])


def test_a_missing_replay_file_replays_nothing(suite: pytest.Pytester) -> None:
    suite.makepyfile("async def test_sim(sim):\n    pass\n")
    missing = suite.path / "missing.jsonl"

    result = suite.runpytest_inprocess("--sim-explore=2", f"--sim-explore-replay={missing}")

    result.assert_outcomes(passed=1)
    assert "replay file ignored" not in result.stdout.str()


_TWO_ROWS = (
    '{"test": "test_x.py::test_sim", "seed": 1}\n{"test": "test_x.py::test_sim", "seed": 2}\n'
)


@pytest.mark.parametrize(
    "cache",
    [
        pytest.param(_TWO_ROWS[:-10], id="cut-short-in-the-last-row"),
        pytest.param(_TWO_ROWS[:20], id="cut-short-in-the-first-row"),
        pytest.param('{"test": "test_x.py::test_sim", "seed": 1, "salt": "x"}\n', id="unknown-key"),
        pytest.param('{"test": "test_x.py::test_sim", "seed": true}\n', id="seed-not-an-integer"),
        pytest.param("[1, 2]\n", id="not-an-object"),
        pytest.param("\x00\x00\n", id="garbage"),
    ],
)
def test_an_unreadable_replay_file_is_ignored_with_a_note_so_the_run_rewrites_it(
    suite: pytest.Pytester, cache: str
) -> None:
    suite.makepyfile("async def test_sim(sim):\n    pass\n")
    bad = suite.path / "bad.jsonl"
    bad.write_text(cache)
    report = suite.path / "report.jsonl"

    result = suite.runpytest_inprocess(
        "--sim-explore=2", f"--sim-explore-replay={bad}", f"--sim-explore-report={report}"
    )

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines([f"*replay file ignored, replaying nothing: {bad}:*"])
    [row] = [json.loads(line) for line in report.read_text().splitlines()]
    assert row["runs"] == 2


def _explored(report: Path) -> dict[str, tuple[int, int, str]]:
    """Each test's exploration runs, planned runs and outcome, from ``--sim-explore-report``."""
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    return {row["test"]: (row["runs"], row["planned"], row["outcome"]) for row in rows}


@pytest.mark.parametrize(
    ("arguments", "chosen"), [([], "pr"), (["--depth=pr"], "pr"), (["--depth=nightly"], "nightly")]
)
def test_the_depth_option_sets_the_one_depth_of_the_session(
    suite: pytest.Pytester, arguments: list[str], chosen: str
) -> None:
    suite.makepyfile(
        """
        from vs_sim.api.testing import depth, examples, swept

        def test_depth():
            print("DEPTH", depth().value, examples(pr=3, nightly=12), *swept(10, 3))
        """
    )
    result = suite.runpytest_inprocess("-s", *arguments)

    result.assert_outcomes(passed=1)
    deep = chosen == "nightly"
    swept_points = "0 1 2 3 4 5 6 7 8 9" if deep else "0 4 9"
    result.stdout.fnmatch_lines([f"*DEPTH {chosen} {12 if deep else 3} {swept_points}*"])


@pytest.mark.parametrize("value", ["", "all", "sampled", "NIGHTLY", "deep"])
def test_an_unknown_depth_is_rejected_naming_the_option(suite: pytest.Pytester, value: str) -> None:
    suite.makepyfile("def test_one():\n    pass\n")
    result = suite.runpytest_inprocess(f"--depth={value}")

    assert result.ret != 0
    result.stderr.fnmatch_lines(["*argument --depth: invalid choice: *"])


def test_the_removed_crash_points_option_is_rejected(suite: pytest.Pytester) -> None:
    suite.makepyfile("def test_one():\n    pass\n")
    result = suite.runpytest_inprocess("--crash-points=all")

    assert result.ret != 0
    result.stderr.fnmatch_lines(["*unrecognized arguments: --crash-points=all*"])


@pytest.mark.parametrize(
    ("arguments", "outcomes"),
    [
        ([], {"passed": 1, "deselected": 1}),
        (["--depth=nightly"], {"passed": 2}),
        (["-m", "slow"], {"passed": 1, "deselected": 1}),
        (["--depth=nightly", "-m", "not slow"], {"passed": 1, "deselected": 1}),
    ],
)
def test_slow_tests_run_at_nightly_depth_and_when_selected_by_marker(
    suite: pytest.Pytester, arguments: list[str], outcomes: dict[str, int]
) -> None:
    suite.makeini(
        "[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider -p no:tach\nmarkers = slow: s\n"
    )
    suite.makepyfile(
        """
        import pytest

        def test_fast():
            pass

        @pytest.mark.slow
        def test_slow():
            pass
        """
    )
    result = suite.runpytest_inprocess(*arguments)

    result.assert_outcomes(**outcomes)


_SMOKE_SUITE = """
import pytest

@pytest.mark.slow
@pytest.mark.parametrize(
    "case",
    [pytest.param("known", marks=pytest.mark.xfail(strict=True, reason="known")), "first", "second"],
)
def test_slow_sweep(case):
    assert case != "known"

@pytest.mark.slow
def test_slow_single():
    pass

def test_fast():
    pass
"""


def test_a_pull_request_runs_one_case_of_each_slow_test_in_a_smoke_directory(
    suite: pytest.Pytester,
) -> None:
    suite.makeini(
        "[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider -p no:tach\n"
        "markers = slow: s\nsim_smoke_slow =\n    smoke\n"
    )
    suite.makepyfile(**{"smoke/test_in": _SMOKE_SUITE, "elsewhere/test_out": _SMOKE_SUITE})

    result = suite.runpytest_inprocess("-v")

    ran = re.findall(r"^(\S+::\S+) PASSED", result.stdout.str(), re.MULTILINE)
    assert sorted(ran) == [
        "elsewhere/test_out.py::test_fast",
        "smoke/test_in.py::test_fast",
        "smoke/test_in.py::test_slow_single",
        "smoke/test_in.py::test_slow_sweep[first]",
    ]


def test_the_nightly_depth_runs_every_case_of_a_smoke_directory(suite: pytest.Pytester) -> None:
    suite.makeini(
        "[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider -p no:tach\n"
        "markers = slow: s\nsim_smoke_slow =\n    smoke\n"
    )
    suite.makepyfile(**{"smoke/test_in": _SMOKE_SUITE})

    suite.runpytest_inprocess("--depth=nightly").assert_outcomes(passed=4, xfailed=1)


def test_a_test_whose_product_code_swallows_a_refused_process_start_still_fails(
    suite: pytest.Pytester,
) -> None:
    suite.makepyfile(
        """
        from pathlib import Path

        from vs_sim.api import ProcessSpec
        from vs_sim.api.testing import NO_PROCESSES, FakePipedProcessLauncher

        def best_effort_cleanup(run):
            try:
                run()
            except Exception:
                return "cleanup skipped"
            return "cleaned"

        def test_swallowed_run_refusal():
            spec = ProcessSpec(argv=("docker", "rm", "x"))
            assert best_effort_cleanup(lambda: NO_PROCESSES.run(spec, timeout=None)) == "cleanup skipped"

        def test_swallowed_piped_refusal():
            launcher = FakePipedProcessLauncher()
            assert best_effort_cleanup(lambda: launcher.start(("git", "status"))) == "cleanup skipped"

        def test_no_refusal():
            assert best_effort_cleanup(lambda: None) == "cleaned"
        """
    )
    result = suite.runpytest_inprocess()
    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(["*turned away a process start*docker rm x*"])
    result.stdout.fnmatch_lines(["*turned away a process start*'git' was started*"])
