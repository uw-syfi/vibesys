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
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest
from _pytest.runner import runtestprotocol

# The contract suites are test code: let pytest show their assertions with introspection.
pytest.register_assert_rewrite("vs_sim.contracts")

from vs_sim.api.testing import (  # noqa: E402  # LW-163810 [E402]; the assert-rewrite registration must run before vs_sim.contracts is first imported, which this import does.
    SEED_OPTION,
    EventTrace,
    Sim,
    replay_hint,
    run_virtual,
    seed_for_test,
)

if TYPE_CHECKING:
    from collections.abc import Generator

SIM_ATTRIBUTE = "_vs_sim"
DEFAULT_REAL_TIERS = ["tests/e2e", "tests/slurm_cluster", "tests/minimal_container"]
_TRACES = pytest.StashKey[list[EventTrace]]()


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


def _is_async(item: pytest.Item) -> bool:
    function = getattr(item, "obj", None)
    return inspect.iscoroutinefunction(function)


def _is_sim_test(item: pytest.Item) -> bool:
    """Whether the plugin owns the test: it asks for ``sim``, or it is an unmarked deterministic async test."""
    if not isinstance(item, pytest.Function):
        return False
    if "sim" in item.fixturenames or item.get_closest_marker("sim") is not None:
        return True
    return (
        _is_async(item) and not _in_real_tier(item) and item.get_closest_marker("asyncio") is None
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Reject a test that asks for two schedulers."""
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


def _sim_for(item: pytest.Item) -> Sim:
    existing = getattr(item, SIM_ATTRIBUTE, None)
    if existing is not None:
        return existing
    seed = seed_for_test(item.nodeid, item.config.getoption("sim_seed"))
    trace = EventTrace() if item.config.getoption("sim_determinism_check") else None
    sim = Sim(seed=seed, trace=trace)
    setattr(item, SIM_ATTRIBUTE, sim)
    return sim


@pytest.fixture
def sim(request: pytest.FixtureRequest) -> Sim:
    """This test's simulation: virtual clock, seed, gates, child processes and registered worlds."""
    return _sim_for(request.node)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> object | None:
    """Run an async sim test on the virtual loop."""
    if not (_is_sim_test(pyfuncitem) and _is_async(pyfuncitem)):
        return None
    sim_for_test = _sim_for(pyfuncitem)
    parameters = inspect.signature(pyfuncitem.obj).parameters
    arguments = {name: pyfuncitem.funcargs[name] for name in parameters}
    run_virtual(sim_for_test.clock, pyfuncitem.obj(**arguments), trace=sim_for_test.trace)
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
            ("vs-sim", f"seed {sim_for_test.seed}; replay with {replay_hint(sim_for_test.seed)}")
        )
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
    """With ``--sim-determinism-check``, run a sim test twice and compare the traces."""
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
