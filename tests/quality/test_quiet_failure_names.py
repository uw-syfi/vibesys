"""A quiet xdist session names each failure when it is reported.

Run 39735353042, job 119679636621, cancelled the nightly ``full-depth`` job
before pytest printed its summary. ``-q`` had recorded seven failures as ``F``
and kept the node ids for that summary, so the log never named them.
``-rfE`` writes the same summary and is lost the same way. The line has to be
written from the controller as the report arrives.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from scripts.pytest_hang_dump import failure_notice, pytest_runtest_logreport, reporting_as
from tests.support.child_pytest import BARE_PYTEST

pytest_plugins = ["pytester"]

_Phase = Literal["setup", "call", "teardown"]


class _Role(StrEnum):
    CONTROLLER = "controller"
    WORKER = "worker"


class _Outcome(StrEnum):
    FAILED = "failed"
    PASSED = "passed"


# The cancelled run did not name its seven failures. This is that report: a
# call failure on the quiet controller, which is the only process whose write
# reaches the log before the summary.
_REPORTED = "tests/quality/test_quiet_failure_names.py::test_reported"
_NODEIDS = st.text(
    alphabet=st.characters(exclude_characters="\r\n"),
    min_size=1,
    max_size=80,
)


def _report(nodeid: str, *, when: _Phase, outcome: _Outcome) -> pytest.TestReport:
    return pytest.TestReport(
        nodeid=nodeid,
        location=(nodeid.split("::", 1)[0], 0, nodeid),
        keywords={},
        outcome=outcome.value,
        longrepr=None,
        when=when,
    )


@pytest.mark.regression
@example(nodeid=_REPORTED, when="call", outcome=_Outcome.FAILED, verbose=-1, role=_Role.CONTROLLER)
@example(nodeid=_REPORTED, when="setup", outcome=_Outcome.FAILED, verbose=0, role=_Role.CONTROLLER)
@example(
    nodeid=_REPORTED, when="teardown", outcome=_Outcome.FAILED, verbose=-1, role=_Role.CONTROLLER
)
@example(nodeid=_REPORTED, when="call", outcome=_Outcome.FAILED, verbose=-1, role=_Role.WORKER)
@example(nodeid=_REPORTED, when="call", outcome=_Outcome.FAILED, verbose=1, role=_Role.CONTROLLER)
@example(nodeid=_REPORTED, when="call", outcome=_Outcome.PASSED, verbose=-1, role=_Role.CONTROLLER)
@given(
    nodeid=_NODEIDS,
    when=st.sampled_from(["setup", "call", "teardown"]),
    outcome=st.sampled_from(_Outcome),
    verbose=st.integers(min_value=-2, max_value=3),
    role=st.sampled_from(_Role),
)
def test_a_quiet_controller_names_each_failure_as_it_is_reported(
    nodeid: str,
    when: _Phase,
    outcome: _Outcome,
    verbose: int,
    role: _Role,
) -> None:
    # The reported loss is a quiet (-q, verbosity -1) xdist controller. The
    # same one-character reporter is the default (0). A worker must not print:
    # its output is captured until the summary. -v already prints the node id.
    notice = failure_notice(
        _report(nodeid, when=when, outcome=outcome),
        verbose=verbose,
        worker=role is _Role.WORKER,
    )
    failed = outcome is _Outcome.FAILED

    if role is _Role.WORKER or verbose >= 1 or not failed:
        assert notice is None
        return
    assert notice == f"{'FAILED' if when == 'call' else 'ERROR'} {nodeid}\n"


def test_the_hook_writes_the_notice_before_any_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(_REPORTED, when="call", outcome=_Outcome.FAILED)
    with reporting_as(verbose=-1, worker=False):
        pytest_runtest_logreport(report)
    captured = capsys.readouterr()
    assert captured.err == f"FAILED {_REPORTED}\n"
    assert captured.out == ""

    with reporting_as(verbose=-1, worker=True):
        pytest_runtest_logreport(report)
    with reporting_as(verbose=1, worker=False):
        pytest_runtest_logreport(report)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("cancel", [False, True])
def test_a_quiet_session_names_failures_before_cancellation(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, cancel: bool
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    pytester.makeconftest(
        f"""
        import pytest

        @pytest.hookimpl(trylast=True)
        def pytest_runtest_logreport(report):
            if {cancel!r} and report.failed:
                pytest.exit("cancelled before the summary")
        """
    )
    pytester.makepyfile(
        test_names="""
        import pytest

        def test_fails():
            raise AssertionError("boom")

        def test_passes():
            assert True

        @pytest.fixture
        def broken():
            raise RuntimeError("setup")

        def test_setup(broken):
            assert True
        """
    )

    with reporting_as(verbose=-1, worker=False):
        result = pytester.runpytest_inprocess(
            *BARE_PYTEST,
            "-p",
            "scripts.pytest_hang_dump",
            "-q",
            "--tb=no",
            "-r",
            "",
            "test_names.py",
        )

    named = [
        line for line in result.stderr.str().splitlines() if line.startswith(("FAILED ", "ERROR "))
    ]
    expected = {"FAILED test_names.py::test_fails"}
    if not cancel:
        expected.add("ERROR test_names.py::test_setup")
    assert set(named) == expected
    assert len(named) == len(expected)
    assert "test_passes" not in result.stderr.str()
    assert result.ret != 0
    if cancel:
        assert "short test summary info" not in result.stdout.str()
