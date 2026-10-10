"""The vs-sim pytest plugin: deterministic by default.

Registered by the repository's root ``conftest.py`` (``pytest_plugins``), not by an entry
point: the ``vibesys`` wheel bundles ``vs_sim``, and an entry point would load this plugin
into the pytest session of everyone who installs it. The plugin imports pytest, so it lives
beside the library (``libs/vs-sim/pytest_plugin``) and not inside the standard-library-only
``vs_sim`` package.

What it does:

* **Async tests run on the virtual loop.** A ``async def`` test outside the real-system
  tiers (``sim_real_tiers``) that carries no ``pytest.mark.asyncio`` runs through
  ``vs_sim.api.testing.run_virtual``: sleeping costs no wall time, and a test that waits on
  nothing raises ``VirtualDeadlockError`` at once. A test marked ``asyncio`` stays with
  pytest-asyncio (the two never share a test; marking a test that also asks for ``sim`` is an
  error). Pytest cannot run an unmarked async test at all, so no existing test changes.
* **The ``sim`` fixture** is the test's :class:`vs_sim.api.testing.Sim`.
* **Seeds.** Every sim test has a seed (a pure function of its node id unless
  ``--sim-seed=N`` overrides it). A failure prints the seed and the option that replays it.
* **``--sim-determinism-check``** runs each sim test twice with the same seed and compares
  the :class:`~vs_sim.api.testing.EventTrace` of the two runs (clock advances, task steps
  and input from outside the simulation), failing the second run at the first difference.
* **Exploration** (``--sim-explore=N``, used by the pull-request CI step). Each selected sim
  test runs ``N`` more times, every run under its own seed *and* its own schedule seed, so
  work that is ready at the same time runs in a different order each time (see
  ``run_virtual``). A test that passes only in the default order fails here, and the failure
  prints ``--sim-seed=S --sim-schedule-seed=S`` to replay that exact run. Other tests (not
  sim, not in a real-system tier) run ``--sim-repeat=K`` times to expose flakiness; real-system
  tiers are deselected. ``--sim-explore-budget=SECONDS`` stops starting extra runs once that
  much wall time has passed (the first run of every test always happens), and the terminal
  summary reports how many runs happened and how long they took.
"""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING

import hypothesis
import pytest
from _pytest.runner import runtestprotocol

# The contract suites are test code: let pytest show their assertions with introspection.
pytest.register_assert_rewrite("vs_sim.contracts")

from vs_sim.api.testing import (  # noqa: E402  # LW-163810 [E402]; the assert-rewrite registration must run before vs_sim.contracts is first imported, which this import does.
    SCHEDULE_SEED_OPTION,
    SEED_OPTION,
    EventTrace,
    Sim,
    explore_seed,
    replay_hint,
    seed_for_test,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator

SIM_ATTRIBUTE = "_vs_sim"
_ORIGINAL_INNER = "_vs_sim_inner_test"
DEFAULT_REAL_TIERS = ["tests/e2e", "tests/slurm_cluster", "tests/minimal_container"]
_RUN = pytest.StashKey[int]()
"""The exploration run (from 0) a sim test is currently in; absent outside exploration."""
_EXPLORATION = pytest.StashKey["_Exploration"]()


class _Exploration:
    """What exploration planned, did and skipped, for the terminal summary and the budget."""

    def __init__(self, budget: float | None) -> None:
        self.started = time.monotonic()
        self.budget = budget
        self.planned = 0
        self.done = 0
        self.skipped = 0

    def out_of_budget(self) -> bool:
        return self.budget is not None and time.monotonic() - self.started >= self.budget


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the options and the ini key."""
    group = parser.getgroup("vs-sim", "deterministic simulation")
    group.addoption(
        SEED_OPTION,
        type=int,
        default=None,
        help="run every sim test under exactly this seed (a failure prints the one to use)",
    )
    group.addoption(
        "--sim-determinism-check",
        action="store_true",
        default=False,
        help="run each sim test twice with the same seed and compare their event traces",
    )
    group.addoption(
        SCHEDULE_SEED_OPTION,
        type=int,
        default=None,
        help="break scheduling ties in every sim test by this seed (a failure prints the one to use)",
    )
    group.addoption(
        "--sim-explore",
        type=int,
        default=0,
        metavar="N",
        help="run each selected sim test N times under different seeds and schedules",
    )
    group.addoption(
        "--sim-repeat",
        type=int,
        default=0,
        metavar="K",
        help="with --sim-explore, run each selected non-sim test K times; real-system tiers are deselected",
    )
    group.addoption(
        "--sim-explore-budget",
        type=float,
        default=None,
        metavar="SECONDS",
        help="with --sim-explore, start no extra run after this many wall seconds",
    )
    parser.addini(
        "sim_real_tiers",
        type="linelist",
        default=list(DEFAULT_REAL_TIERS),
        help="directories (relative to the rootdir) of real-system tests, never run virtually",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Declare the marker."""
    config.addinivalue_line(
        "markers", "sim: run this test as a simulation even when it is not an async test"
    )


def pytest_report_header(config: pytest.Config) -> str:
    """Say which seed policy and checks are active."""
    seed = config.getoption("sim_seed")
    policy = "per-test seeds" if seed is None else f"seed {seed} for every sim test"
    check = ", determinism check on" if config.getoption("sim_determinism_check") else ""
    return f"vs-sim: {policy}{check}"


def _in_real_tier(item: pytest.Item) -> bool:
    tiers = item.config.getini("sim_real_tiers")
    try:
        relative = item.path.relative_to(item.config.rootpath).as_posix()
    except ValueError:
        return False
    return any(relative == tier or relative.startswith(f"{tier}/") for tier in tiers)


def _hypothesis_inner(item: pytest.Item) -> Callable[..., Coroutine[object, object, object]] | None:
    """The coroutine function under a ``@given`` wrapper, or ``None`` for any other test."""
    handle = getattr(getattr(item, "obj", None), "hypothesis", None)
    if handle is None:
        return None
    inner = getattr(handle, _ORIGINAL_INNER, handle.inner_test)
    return inner if inspect.iscoroutinefunction(inner) else None


def _is_async(item: pytest.Item) -> bool:
    function = getattr(item, "obj", None)
    return inspect.iscoroutinefunction(function) or _hypothesis_inner(item) is not None


def _is_sim_test(item: pytest.Item) -> bool:
    """Whether the plugin owns the test: it asks for ``sim``, or it is an unmarked deterministic async test."""
    if not isinstance(item, pytest.Function):
        return False
    if "sim" in item.fixturenames or item.get_closest_marker("sim") is not None:
        return True
    return (
        _is_async(item) and not _in_real_tier(item) and item.get_closest_marker("asyncio") is None
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Reject a test that asks for two schedulers; when exploring, keep only what can be repeated."""
    if config.getoption("sim_explore") > 0:
        repeatable = [item for item in items if _is_explorable(item)]
        config.hook.pytest_deselected(items=[item for item in items if item not in repeatable])
        items[:] = repeatable
    for item in items:
        if (
            isinstance(item, pytest.Function)
            and ("sim" in item.fixturenames or item.get_closest_marker("sim") is not None)
            and item.get_closest_marker("asyncio") is not None
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "uses the sim fixture and pytest.mark.asyncio: drop the asyncio marker, "
                        "the sim fixture runs async tests on the virtual loop"
                    )
                )
            )
            item.config.issue_config_time_warning(
                pytest.PytestWarning(f"{item.nodeid}: sim and asyncio markers together"), 2
            )


def _is_explorable(item: pytest.Item) -> bool:
    """Exploration repeats sim tests under new seeds, and repeats other tests outside the real tiers."""
    if _is_sim_test(item):
        return True
    return (
        item.config.getoption("sim_repeat") > 0
        and isinstance(item, pytest.Function)
        and not _in_real_tier(item)
    )


def _sim_for(item: pytest.Item) -> Sim:
    existing = getattr(item, SIM_ATTRIBUTE, None)
    if existing is not None:
        return existing
    run = item.stash.get(_RUN, None)
    if run is None:
        seed = seed_for_test(item.nodeid, item.config.getoption("sim_seed"))
        schedule_seed = item.config.getoption("sim_schedule_seed")
    else:
        seed = schedule_seed = explore_seed(item.nodeid, run)
    trace = EventTrace() if item.config.getoption("sim_determinism_check") else None
    sim = Sim(seed=seed, trace=trace, schedule_seed=schedule_seed)
    setattr(item, SIM_ATTRIBUTE, sim)
    return sim


@pytest.fixture
def sim(request: pytest.FixtureRequest) -> Sim:
    """This test's simulation: virtual clock, seed, gates, child processes and registered worlds."""
    return _sim_for(request.node)


def _seed_hypothesis(item: pytest.Function, sim_for_test: Sim) -> None:
    """Draw a ``@given`` test's examples from the sim seed, so two runs see the same examples."""
    function = item.obj
    if getattr(function, "hypothesis", None) is None:
        return
    if getattr(function, "_hypothesis_internal_use_seed", None) is not None:
        return  # the test pins its own seed
    hypothesis.seed(sim_for_test.seed)(function)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> object | None:
    """Run an async sim test on the virtual loop."""
    if not _is_sim_test(pyfuncitem):
        return None
    sim_for_test = _sim_for(pyfuncitem)
    _seed_hypothesis(pyfuncitem, sim_for_test)
    if not _is_async(pyfuncitem):
        return None
    inner = _hypothesis_inner(pyfuncitem)
    if inner is not None:
        # Hypothesis calls its inner test once per example; each call is a virtual run.
        handle = pyfuncitem.obj.hypothesis

        def run_example(*args: object, **kwargs: object) -> None:
            sim_for_test.run(inner(*args, **kwargs))

        setattr(handle, _ORIGINAL_INNER, inner)
        handle.inner_test = run_example
        return None
    parameters = inspect.signature(pyfuncitem.obj).parameters
    arguments = {name: pyfuncitem.funcargs[name] for name in parameters}
    sim_for_test.run(pyfuncitem.obj(**arguments))
    return True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Print the seed of a failing sim test with the option that replays it."""
    report = yield
    sim_for_test = getattr(item, SIM_ATTRIBUTE, None)
    if report.failed and sim_for_test is not None:
        report.sections.append(
            (
                "vs-sim",
                f"seed {sim_for_test.seed}; replay with "
                f"{replay_hint(sim_for_test.seed, sim_for_test.schedule_seed)}",
            )
        )
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
    """Run a sim test twice and compare traces (``--sim-determinism-check``), or explore it (``--sim-explore``)."""
    if item.config.getoption("sim_explore") > 0 and isinstance(item, pytest.Function):
        return _explore(item, nextitem)
    if not item.config.getoption("sim_determinism_check") or not _is_sim_test(item):
        return None
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    first = runtestprotocol(item, nextitem=nextitem, log=False)
    first_trace = _take_trace(item)
    if not all(report.passed or report.skipped for report in first) or first_trace is None:
        _log(item, first)
        item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True
    second = runtestprotocol(item, nextitem=nextitem, log=False)
    second_trace = _take_trace(item)
    difference = (
        None
        if second_trace is None or not all(r.passed for r in second)
        else first_trace.first_difference(second_trace)
    )
    if difference is not None:
        second = _with_difference(second, difference)
    _log(item, second)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


def _exploration(config: pytest.Config) -> _Exploration:
    state = config.stash.get(_EXPLORATION, None)
    if state is None:
        state = _Exploration(config.getoption("sim_explore_budget"))
        config.stash[_EXPLORATION] = state
    return state


def _explore(item: pytest.Function, nextitem: pytest.Item | None) -> bool:
    """Run the test ``N`` (sim) or ``K`` (other) times, stopping at the first failure or the budget."""
    config = item.config
    state = _exploration(config)
    is_sim = _is_sim_test(item)
    runs = config.getoption("sim_explore" if is_sim else "sim_repeat")
    state.planned += runs
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    reports: list[pytest.TestReport] = []
    for run in range(runs):
        # The first run of a test always happens: a budget bounds the extra work, not coverage.
        if run > 0 and state.out_of_budget():
            state.skipped += runs - run
            break
        if is_sim:
            item.stash[_RUN] = run
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        state.done += 1
        if not all(report.passed or report.skipped for report in reports):
            state.skipped += runs - run - 1
            break
        if getattr(item, SIM_ATTRIBUTE, None) is not None:
            delattr(item, SIM_ATTRIBUTE)
    _log(item, reports)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Report what exploration ran, so its cost is visible in every log."""
    state = config.stash.get(_EXPLORATION, None)
    if state is None:
        return
    elapsed = time.monotonic() - state.started
    terminalreporter.write_line(
        f"vs-sim exploration: {state.done} of {state.planned} planned runs in {elapsed:.1f} s"
        f" ({state.skipped} not run: budget or an earlier failure)"
    )


def _take_trace(item: pytest.Item) -> EventTrace | None:
    sim_for_test = getattr(item, SIM_ATTRIBUTE, None)
    if sim_for_test is None:
        return None
    trace = sim_for_test.trace
    delattr(item, SIM_ATTRIBUTE)
    return trace


def _with_difference(reports: list[pytest.TestReport], difference: str) -> list[pytest.TestReport]:
    message = (
        "the test is not deterministic: two runs with the same seed left different event "
        f"traces ({difference}). A wall-clock read, a real thread or socket, or an unseeded "
        "random draw decided something."
    )
    call = next(report for report in reports if report.when == "call")
    call.outcome = "failed"
    call.longrepr = message
    return reports


def _log(item: pytest.Item, reports: list[pytest.TestReport]) -> None:
    for report in reports:
        item.ihook.pytest_runtest_logreport(report=report)
