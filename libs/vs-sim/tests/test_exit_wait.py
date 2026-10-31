"""Every way this host waits for a child to exit passes one contract, and the choice is by capability.

The host's own waiter always runs; a mechanism the host lacks cannot run, so its class is
absent here rather than skipped (``waitid`` is on Linux, ``kqueue`` on macOS).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import (
    ExitWaitUnsupportedError,
    KqueueExitWaiter,
    WaitidExitWaiter,
    host_exit_waiter,
)
from vs_sim.api.testing import ExitWaiterContract

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import ExitWaiter


def _provided(mechanism: Callable[[], ExitWaiter]) -> bool:
    try:
        mechanism()
    except ExitWaitUnsupportedError:
        return False
    return True


class TestHostExitWaiter(ExitWaiterContract):
    def waiter(self) -> ExitWaiter:
        return host_exit_waiter()


if _provided(WaitidExitWaiter):

    class TestWaitidExitWaiter(ExitWaiterContract):
        def waiter(self) -> ExitWaiter:
            return WaitidExitWaiter()


if _provided(KqueueExitWaiter):

    class TestKqueueExitWaiter(ExitWaiterContract):
        def waiter(self) -> ExitWaiter:
            return KqueueExitWaiter()


def test_this_host_provides_a_way_to_wait_for_an_exit_without_reaping() -> None:
    """Linux has ``waitid`` and macOS has ``kqueue``; a host with neither is not supported."""
    assert _provided(WaitidExitWaiter) or _provided(KqueueExitWaiter)


class _Missing:
    """A mechanism the host lacks, named by *why*."""

    def __init__(self, why: str) -> None:
        self.why = why

    def __call__(self) -> ExitWaiter:
        raise ExitWaitUnsupportedError(self.why)


class _Present:
    """A mechanism the host has, which waits by doing nothing."""

    def wait(self, pid: int) -> None:
        del pid

    def __call__(self) -> ExitWaiter:
        return self


_REASONS = st.text(alphabet=st.characters(codec="ascii", categories=("L", "N")), min_size=1)


@given(missing=st.lists(_REASONS, max_size=4, unique=True), later=st.lists(_REASONS, max_size=3))
def test_the_first_mechanism_the_host_provides_is_the_one_chosen(
    missing: list[str], later: list[str]
) -> None:
    present = _Present()

    chosen = host_exit_waiter((*map(_Missing, missing), present, *map(_Missing, later)))

    assert chosen is present


@given(missing=st.lists(_REASONS, max_size=4, unique=True))
def test_a_host_with_no_mechanism_fails_at_the_wait_and_names_every_reason(
    missing: list[str],
) -> None:
    waiter = host_exit_waiter(tuple(map(_Missing, missing)))

    with pytest.raises(ExitWaitUnsupportedError) as raised:
        waiter.wait(1)

    assert all(why in str(raised.value) for why in missing)
