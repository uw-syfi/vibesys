"""The ``vibesys`` launcher never ends the engine it started before it exits.

r14: Ctrl-C reached both the launcher and its headless engine. The launcher's
``subprocess.call`` SIGKILLed the engine 0.25 seconds later, so the engine's
teardown never cancelled the run's Slurm job.

The launcher runs its child through a Fake foreground launcher and receives its signals
from a Fake signal source, so no signal reaches this process. The real processes and
signals are in ``tests/e2e/test_launcher_signals.py``.
"""

from __future__ import annotations

import asyncio
import signal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from entrypoints.launcher import run_child
from vs_sim.api.testing import (
    FakeForegroundLauncher,
    FakeSignalSource,
    ForegroundScript,
    arrival,
)

_ENGINE_STATUS = 7
_HANDLED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_FORWARDED = (signal.SIGTERM, signal.SIGHUP)


def _engine(_argv: tuple[str, ...]) -> ForegroundScript:
    """An engine that survives its signals and ends only when the test lets it."""
    return ForegroundScript(ignored_signals=frozenset(_HANDLED))


async def _run_with_signals(
    delivered: list[signal.Signals],
) -> tuple[int, FakeForegroundLauncher, FakeSignalSource]:
    launcher = FakeForegroundLauncher(_engine)
    signals = FakeSignalSource()
    launch = asyncio.ensure_future(run_child(["engine"], children=launcher, signals=signals))
    engine = await arrival(launcher.child(), launch)
    for number in delivered:
        assert signals.deliver(number), f"the launcher installed no {number.name} handler"
        # The launcher keeps waiting: the engine owns its teardown.
        assert not launch.done()
    engine.exit(_ENGINE_STATUS)
    return await launch, launcher, signals


@given(st.lists(st.sampled_from(_HANDLED), max_size=8))
@pytest.mark.asyncio
async def test_the_launcher_waits_for_its_engine_and_forwards_termination(
    delivered: list[signal.Signals],
) -> None:
    status, launcher, signals = await _run_with_signals(delivered)

    assert status == _ENGINE_STATUS
    # Ctrl-C reaches the engine from the terminal; only termination is forwarded.
    assert launcher.children[0].received == [n for n in delivered if n in _FORWARDED]
    assert not any(signals.handles(number) for number in _HANDLED)


@given(
    st.sampled_from([*signal.Signals]).filter(lambda n: n not in (signal.SIGKILL, signal.SIGSTOP))
)
@pytest.mark.asyncio
async def test_a_child_ended_by_a_signal_reports_128_plus_the_signal(
    number: signal.Signals,
) -> None:
    launcher = FakeForegroundLauncher(lambda _argv: ForegroundScript())
    launch = asyncio.ensure_future(
        run_child(["engine"], children=launcher, signals=FakeSignalSource())
    )
    engine = await arrival(launcher.child(), launch)

    engine.send_signal(number)

    assert await launch == 128 + number


@pytest.mark.asyncio
async def test_the_launcher_returns_the_exit_status_of_a_child_that_ends_at_once() -> None:
    launcher = FakeForegroundLauncher(
        lambda _argv: ForegroundScript(returncode=3, exits_immediately=True)
    )

    status = await run_child(["engine"], children=launcher, signals=FakeSignalSource())

    assert status == 3


@pytest.mark.asyncio
async def test_a_child_that_cannot_start_leaves_no_handlers_behind() -> None:
    def missing(argv: tuple[str, ...]) -> ForegroundScript:
        raise FileNotFoundError(argv[0])

    signals = FakeSignalSource()

    with pytest.raises(FileNotFoundError):
        await run_child(
            ["no-such-program"], children=FakeForegroundLauncher(missing), signals=signals
        )

    assert not any(signals.handles(number) for number in _HANDLED)
