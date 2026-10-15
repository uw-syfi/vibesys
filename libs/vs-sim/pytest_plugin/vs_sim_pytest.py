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
* **``--depth``** (``pr``, the default, or ``nightly``) is the session's one search depth
  (`vs_sim.api.testing.depth`). A pull request samples: crash sweeps try a sample of their
  points (`swept`), property tests draw ``examples(pr=..., nightly=...)``, and tests marked
  ``slow`` are deselected, except one smoke case of each slow test under the directories of
  ``sim_smoke_slow``, so a regression only a slow test catches fails the pull request that
  causes it, not the next nightly run (the smoke case is the first collected case that is
  not marked ``xfail``, so it is the same case every run). The nightly job explores: every crash point, the larger example
  counts, and the ``slow`` tests. An explicit ``-m`` expression naming ``slow`` keeps them at
  either depth. The repository's root ``conftest.py`` picks the Hypothesis profile from it.
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
  tiers are deselected. Exploration runs in rounds: round ``r`` runs every test that has runs
  left once, so a budget that ends early leaves the tests explored about equally rather than
  the first few deeply and the rest not at all. ``--sim-explore-budget=SECONDS`` (counted from
  the session's start) stops starting runs, including a test's first exploration run: the
  ordinary suite has already run every test once under its default seed. An interrupt
  (Ctrl-C, or the CI step's hard cap) ends exploration the same way and keeps the results so
  far. ``--sim-explore-salt=TEXT`` picks which seeds the runs after the first
  ``FIXED_EXPLORATION_RUNS`` use, and which test each round starts from, so successive
  commits explore different seeds and, under a tight budget, different tests. The first
  ``FIXED_EXPLORATION_RUNS`` runs of each test use the same seeds under every salt, so a
  seed that failed on one push runs again on the next. A test the budget left unexplored is reported as skipped.
  ``--sim-explore-report=PATH`` writes one JSON object per selected test (``test``: node id,
  ``runs``: exploration runs done, ``planned``: runs asked for, ``outcome``: ``passed``,
  ``failed``, ``skipped`` or ``not explored``, ``seed``: the seed of a sim test's failing
  run, else ``null``) for the CI step's job summary.
  ``--sim-explore-replay=PATH`` reads earlier failures (JSON lines of ``test`` and ``seed``,
  as the report writes them; a missing file replays nothing) and gives each listed test's
  first runs those seeds, so a failure found under any salt is retried first on the next
  push; the test's usual seeds follow. The terminal
  summary reports how many runs happened and how long they took.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import hypothesis
import pydantic
import pytest
from _pytest.runner import runtestprotocol

# The contract suites are test code: let pytest show their assertions with introspection.
pytest.register_assert_rewrite("vs_sim.contracts")

from vs_sim.api.testing import (  # noqa: E402  # LW-163810 [E402]; the assert-rewrite registration must run before vs_sim.contracts is first imported, which this import does.
    SCHEDULE_SEED_OPTION,
    SEED_OPTION,
    Depth,
    EventTrace,
    Sim,
    explore_seed,
    refused_starts,
    replay_hint,
    seed_for_test,
    set_depth,
    undriven_threads,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator

SIM_ATTRIBUTE = "_vs_sim"
DEPTH_OPTION = "--depth"
_ORIGINAL_INNER = "_vs_sim_inner_test"
DEFAULT_REAL_TIERS = ["tests/e2e", "tests/slurm_cluster", "tests/minimal_container"]
_RUN = pytest.StashKey[int]()
"""The exploration run (from 0) a sim test is currently in; absent outside exploration."""
_EXPLORATION = pytest.StashKey["_Exploration"]()
FIXED_EXPLORATION_RUNS = 2
"""Each test's first exploration runs use seeds no salt changes, so re-pushing retries them."""


class _Exploration:
    """What exploration planned, did and skipped, for the terminal summary and the budget."""

    def __init__(self, config: pytest.Config) -> None:
        self.started = time.monotonic()
        self.budget: float | None = config.getoption("sim_explore_budget")
        salt: str | None = config.getoption("sim_explore_salt")
        self.offset = 0 if salt is None else seed_for_test(f"explore-salt:{salt}")
        self.planned = 0
        self.done = 0
        self.skipped = 0
        self.stopped_by_interrupt = False
        self.outcomes: list[dict[str, object]] = []
        replay: str | None = config.getoption("sim_explore_replay")
        self.replays, self.replay_ignored = (
            _read_replays(Path(replay)) if replay is not None else ({}, None)
        )

    def seed(self, test: str, run: int) -> int:
        """The seed of exploration run ``run`` of ``test``: replays first, then the usual seeds."""
        replays = self.replays.get(test, [])
        if run < len(replays):
            return replays[run]
        run -= len(replays)
        return explore_seed(test, run if run < FIXED_EXPLORATION_RUNS else self.offset + run)

    def out_of_budget(self) -> bool:
        return self.budget is not None and time.monotonic() - self.started >= self.budget


class _ReplayRow(pydantic.BaseModel):
    """One line of a replay file: a failing test and the seed that failed it."""

    model_config = pydantic.ConfigDict(extra="forbid", strict=True)

    test: str
    seed: int


def _read_replays(path: Path) -> tuple[dict[str, list[int]], str | None]:
    """Each test's failing seeds from a replay file, and why the file was ignored (else ``None``).

    The file is a cache that CI restores between pushes and rewrites from each run, so a file
    that cannot be read (a run killed mid-write truncated it, or an older plugin wrote another
    shape) is ignored whole: failing on it would keep it from ever being rewritten.
    """
    if not path.exists():
        return {}, None
    replays: dict[str, list[int]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = _ReplayRow.model_validate_json(line)
        except pydantic.ValidationError as error:
            keys = sorted(_ReplayRow.model_fields)
            why = error.errors()[0]["msg"]
            return {}, f"{path}:{number}: a replay row has exactly the keys {keys} ({why})"
        replays.setdefault(row.test, []).append(row.seed)
    return replays, None


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
        DEPTH_OPTION,
        choices=[chosen.value for chosen in Depth],
        default=Depth.PR.value,
        help="how deep the session searches: sample (pr) or explore (nightly)",
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
        help="with --sim-explore, start no run after this many wall seconds from the session start",
    )
    group.addoption(
        "--sim-explore-salt",
        default=None,
        metavar="TEXT",
        help="with --sim-explore, vary the seeds and the round order by this text (CI: the commit)",
    )
    group.addoption(
        "--sim-explore-report",
        default=None,
        metavar="PATH",
        help="with --sim-explore, write each test's exploration runs and outcome as JSON lines",
    )
    group.addoption(
        "--sim-explore-replay",
        default=None,
        metavar="PATH",
        help="with --sim-explore, run these earlier failing (test, seed) pairs first (JSON lines)",
    )
    parser.addini(
        "sim_real_tiers",
        type="linelist",
        default=list(DEFAULT_REAL_TIERS),
        help="directories (relative to the rootdir) of real-system tests, never run virtually",
    )
    parser.addini(
        "sim_smoke_slow",
        type="linelist",
        default=[],
        help="directories (relative to the rootdir) whose slow tests still run one case each in a pull request",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Declare the marker and make the chosen depth the session's one setting."""
    set_depth(Depth(config.getoption("depth")))
    if config.getoption("sim_explore") > 0:
        config.stash[_EXPLORATION] = _Exploration(config)
    config.addinivalue_line(
        "markers", "sim: run this test as a simulation even when it is not an async test"
    )


def pytest_report_header(config: pytest.Config) -> str:
    """Say which seed policy and checks are active."""
    seed = config.getoption("sim_seed")
    policy = "per-test seeds" if seed is None else f"seed {seed} for every sim test"
    check = ", determinism check on" if config.getoption("sim_determinism_check") else ""
    return f"vs-sim: {policy}{check}, depth {config.getoption('depth')}"


def _in_real_tier(item: pytest.Item) -> bool:
    return _in_directories(item, "sim_real_tiers")


def _in_directories(item: pytest.Item, key: str) -> bool:
    tiers = item.config.getini(key)
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


def _smoke_cases(slow: list[pytest.Item]) -> list[pytest.Item]:
    """The first case that is not expected to fail of each slow test under ``sim_smoke_slow``."""
    chosen: dict[tuple[object, object, str], pytest.Item] = {}
    for item in slow:
        if not isinstance(item, pytest.Function) or not _in_directories(item, "sim_smoke_slow"):
            continue
        if item.get_closest_marker("xfail") is None:
            chosen.setdefault((item.path, item.cls, item.originalname), item)
    return list(chosen.values())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Reject a test that asks for two schedulers; when exploring, keep only what can be repeated."""
    if Depth(config.getoption("depth")) is Depth.PR and "slow" not in (
        config.option.markexpr or ""
    ):
        slow = [item for item in items if item.get_closest_marker("slow") is not None]
        smoke = _smoke_cases(slow)
        slow = [item for item in slow if item not in smoke]
        config.hook.pytest_deselected(items=slow)
        items[:] = [item for item in items if item not in slow]
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
        seed = schedule_seed = item.config.stash[_EXPLORATION].seed(item.nodeid, run)
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
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    """Fail a test that spawned simulated threads on a simulator nothing drove, or started a refused process."""
    del item
    undriven_threads()
    refused_starts()
    yield
    refused = refused_starts()
    if refused:
        pytest.fail(
            "a refusing Fake turned away a process start (product code may have swallowed "
            f"the error): {'; '.join(refused)}",
            pytrace=False,
        )
    stranded = undriven_threads()
    if stranded:
        pytest.fail(
            f"threads {', '.join(stranded)} were spawned on a SimThreads that nothing drove, "
            "so they never ran; drive it with SimThreads.run or Sim.run(threads=...)",
            pytrace=False,
        )


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
    """Run a sim test twice and compare traces (``--sim-determinism-check``)."""
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


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> bool | None:
    """With ``--sim-explore``, run the selected tests in rounds until their runs or the budget end."""
    state = session.config.stash.get(_EXPLORATION, None)
    if state is None or session.config.option.collectonly:
        return None
    if session.testsfailed and not session.config.option.continue_on_collection_errors:
        return None  # pytest's own loop reports the collection errors
    items = [item for item in session.items if isinstance(item, pytest.Function)]
    if items:
        start = state.offset % len(items)
        items = items[start:] + items[:start]
    _explore_in_rounds(session, state, items)
    report_path = session.config.getoption("sim_explore_report")
    if report_path is not None:
        Path(report_path).write_text(
            "".join(f"{json.dumps(outcome)}\n" for outcome in state.outcomes), encoding="utf-8"
        )
    return True


def _explore_in_rounds(
    session: pytest.Session, state: _Exploration, items: list[pytest.Function]
) -> None:
    """Run round after round; log each test once, when it fails or when exploration ends."""
    config = session.config
    runs = {
        item: config.getoption("sim_explore" if _is_sim_test(item) else "sim_repeat")
        for item in items
    }
    state.planned = sum(runs.values())
    done = dict.fromkeys(items, 0)
    reports: dict[pytest.Function, list[pytest.TestReport]] = {}
    unlogged = list(items)
    try:
        for round_index in range(max(runs.values(), default=0)):
            queue = [item for item in unlogged if round_index < runs[item]]
            for position, item in enumerate(queue):
                if state.out_of_budget():
                    return
                following = queue[position + 1] if position + 1 < len(queue) else None
                reports[item] = _run_once(item, round_index, following)
                done[item] += 1
                state.done += 1
                if not all(report.passed or report.skipped for report in reports[item]):
                    seed = state.seed(item.nodeid, round_index) if _is_sim_test(item) else None
                    _log_exploration(state, item, (done[item], runs[item]), reports[item], seed)
                    unlogged.remove(item)
                if session.shouldfail or session.shouldstop:
                    return
    except KeyboardInterrupt:
        state.stopped_by_interrupt = True
    finally:
        for item in unlogged:
            _log_exploration(state, item, (done[item], runs[item]), reports.get(item))
        state.skipped = state.planned - state.done


def _run_once(
    item: pytest.Function, round_index: int, following: pytest.Item | None
) -> list[pytest.TestReport]:
    """One exploration run: a sim test under its ``round_index``-th exploration seed."""
    if _is_sim_test(item):
        item.stash[_RUN] = round_index
    reports = runtestprotocol(item, nextitem=following, log=False)
    if getattr(item, SIM_ATTRIBUTE, None) is not None:
        delattr(item, SIM_ATTRIBUTE)
    return reports


def _log_exploration(
    state: _Exploration,
    item: pytest.Function,
    runs: tuple[int, int],
    reports: list[pytest.TestReport] | None,
    failing_seed: int | None = None,
) -> None:
    """Log a test's last run (``runs`` is done and planned), or a skip when it never ran.

    ``failing_seed`` is the seed of a sim test's failing run, for the report.
    """
    if reports is None:
        outcome = "not explored"
        reason = "not explored: the budget or an interrupt ended exploration first"
        reports = [
            item.ihook.pytest_runtest_makereport(
                item=item, call=pytest.CallInfo.from_call(lambda: pytest.skip(reason), when="setup")
            ),
            item.ihook.pytest_runtest_makereport(
                item=item, call=pytest.CallInfo.from_call(lambda: None, when="teardown")
            ),
        ]
    elif any(report.failed for report in reports):
        outcome = "failed"
    else:
        outcome = "skipped" if any(report.skipped for report in reports) else "passed"
    done, planned = runs
    state.outcomes.append(
        {
            "test": item.nodeid,
            "runs": done,
            "planned": planned,
            "outcome": outcome,
            "seed": failing_seed,
        }
    )
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    _log(item, reports)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Report what exploration ran, so its cost is visible in every log."""
    state = config.stash.get(_EXPLORATION, None)
    if state is None:
        return
    if state.replay_ignored is not None:
        terminalreporter.write_line(
            f"vs-sim exploration: replay file ignored, replaying nothing: {state.replay_ignored}",
            yellow=True,
        )
    elapsed = time.monotonic() - state.started
    stop = ", stopped by an interrupt" if state.stopped_by_interrupt else ""
    terminalreporter.write_line(
        f"vs-sim exploration: {state.done} of {state.planned} planned runs in {elapsed:.1f} s"
        f" ({state.skipped} not run: budget, interrupt or an earlier failure{stop})"
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
