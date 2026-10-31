"""The deadline dump of a worker reaches the log: it names the test that worker is inside.

Run 41164573639, job 124429800191 (``test (1)``), hit the 10 minute job limit at 99%.
Its log held one dump, the controller's, which said "no test is running". The three tests
that had started and not reported could not be told apart: running, or hung. Each xdist
worker arms its own dump timer, but a worker's stderr is captured while a test runs, so
every dump that named a test was written into the capture and lost with the process.
The dump writes with the capture suspended, as ``capsys.disabled()`` does.
"""

from __future__ import annotations

import inspect

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.pytest_hang_dump import render_dump
from tests.support.child_pytest import BARE_PYTEST

pytest_plugins = ["pytester"]

_NODEIDS = st.text(
    alphabet=st.characters(exclude_characters="\r\n", exclude_categories=["Cs"]),
    min_size=1,
    max_size=80,
)
_PROCESSES = st.sampled_from(["controller", "gw0", "gw1", "gw17"])
_REPORTED = "libs/vs-slurm/tests/test_connector_process_contract.py::test_the_in_process_fake"


@pytest.mark.parametrize("worker", [None, "gw7"])
@pytest.mark.parametrize("capture", ["fd", "sys"])
def test_a_dump_taken_inside_a_running_test_reaches_the_real_stderr(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, capture: str, worker: str | None
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    if worker is None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    pytester.makepyfile(
        test_inner="""
        from scripts.pytest_hang_dump import dump_stacks

        def test_inside_the_running_test():
            dump_stacks()
        """
    )

    result = pytester.runpytest_inprocess(
        *BARE_PYTEST, "-p", "scripts.pytest_hang_dump", f"--capture={capture}", "test_inner.py"
    )

    log = result.stdout.str() + result.stderr.str()
    assert f"dumping every thread of {worker or 'controller'} before the job is cancelled" in log
    assert "running test: test_inner.py::test_inside_the_running_test (running for " in log
    assert "in test_inside_the_running_test" in log
    assert result.ret == 0


def test_a_dump_taken_after_the_last_test_still_reaches_the_real_stderr(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    pytester.makeconftest(
        """
        import pytest
        from scripts.pytest_hang_dump import dump_stacks

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(session):
            dump_stacks()
        """
    )
    pytester.makepyfile(test_inner="def test_passes():\n    assert True\n")

    result = pytester.runpytest_inprocess(
        *BARE_PYTEST, "-p", "scripts.pytest_hang_dump", "test_inner.py"
    )

    log = result.stdout.str() + result.stderr.str()
    assert "dumping every thread of controller before the job is cancelled" in log
    assert "running test:" in log


@pytest.mark.regression
@example(nodeid=_REPORTED, process="controller", running_s=21.0)
@example(nodeid=_REPORTED, process="gw3", running_s=21.0)
@given(nodeid=_NODEIDS, process=_PROCESSES, running_s=st.floats(min_value=0, max_value=1e5))
def test_a_dump_names_its_process_the_running_test_and_how_long_it_has_run(
    nodeid: str, process: str, running_s: float
) -> None:
    # "Running" and "hung" differ only by how long: the report that started this showed
    # three tests that had begun and not reported, with nothing to say for how long.
    frame = inspect.currentframe()
    assert frame is not None

    text = render_dump(nodeid, {1: frame}, {1: "MainThread"}, process=process, running_s=running_s)

    assert text.startswith(
        f"pytest job deadline: dumping every thread of {process} before the job is cancelled\n"
    )
    assert f"running test: {nodeid} (running for {running_s:.0f} s)\n" in text
    assert "Thread MainThread (1):" in text


def test_a_dump_between_tests_has_no_duration() -> None:
    text = render_dump(None, {}, {}, process="gw2", running_s=None)

    assert "running test: (no test is running)\n" in text
    assert "running for" not in text
