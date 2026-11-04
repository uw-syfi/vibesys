"""Lifecycle contract for an environment session that owns host-side brokers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

# Its only production caller constructs real Slurm brokers; fake roles keep this tier deterministic.
# test-isolation: exercise the sibling-owned aggregate close contract without real brokers.
from vs_runtime._brokered_session import BrokeredRunEnvironmentSession
from vs_runtime.api.infrastructure import AgentPaths, RunEnvironmentView
from vs_sandbox.api.testing import FakeCommandRunner

if TYPE_CHECKING:
    from vs_sandbox.api import CommandRunner


class _CloseFailedError(RuntimeError):
    """A scripted transient close failure."""


@dataclass
class _CloseRecord:
    name: str
    order: list[str]
    failures_remaining: int = 0
    attempts: int = 0

    def close(self) -> None:
        self.attempts += 1
        self.order.append(self.name)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise _CloseFailedError(self.name)


@dataclass
class _Session(_CloseRecord):
    sandbox: CommandRunner = field(init=False)
    view: RunEnvironmentView = field(init=False)

    def __post_init__(self) -> None:
        self.sandbox = FakeCommandRunner()
        self.view = RunEnvironmentView(paths=AgentPaths(), workspace_git_writable=True)

    @property
    def capture_commands(self) -> CommandRunner:
        return self.sandbox

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()


def test_close_retries_only_failed_owners_after_attempting_every_owner() -> None:
    """Regression: the old boolean latch and ExitStack forgot the failed callback."""
    order: list[str] = []
    delegate = _Session("delegate", order, failures_remaining=1)
    first = _CloseRecord("broker-1", order)
    second = _CloseRecord("broker-2", order)
    session = BrokeredRunEnvironmentSession(delegate, (first, second))

    with pytest.raises(_CloseFailedError, match="delegate"):
        session.close()

    assert order == ["delegate", "broker-2", "broker-1"]

    session.close()
    session.close()

    assert order == ["delegate", "broker-2", "broker-1", "delegate"]
    assert (delegate.attempts, first.attempts, second.attempts) == (2, 1, 1)


def test_close_preserves_the_first_failure_and_reports_later_failures() -> None:
    order: list[str] = []
    delegate = _Session("delegate", order, failures_remaining=1)
    first = _CloseRecord("broker-1", order)
    second = _CloseRecord("broker-2", order, failures_remaining=1)
    session = BrokeredRunEnvironmentSession(delegate, (first, second))

    with pytest.raises(_CloseFailedError, match="delegate") as raised:
        session.close()

    assert order == ["delegate", "broker-2", "broker-1"]
    assert any("broker-2" in note for note in (raised.value.__notes__ or ()))


@given(
    delegate_failures=st.integers(0, 3),
    first_failures=st.integers(0, 3),
    second_failures=st.integers(0, 3),
    extra_closes=st.integers(0, 2),
)
def test_each_owner_remains_owned_until_its_first_success(
    delegate_failures: int,
    first_failures: int,
    second_failures: int,
    extra_closes: int,
) -> None:
    order: list[str] = []
    delegate = _Session("delegate", order, failures_remaining=delegate_failures)
    first = _CloseRecord("broker-1", order, failures_remaining=first_failures)
    second = _CloseRecord("broker-2", order, failures_remaining=second_failures)
    session = BrokeredRunEnvironmentSession(delegate, (first, second))
    failures = {
        "delegate": delegate_failures,
        "broker-2": second_failures,
        "broker-1": first_failures,
    }

    closes = max(failures.values()) + 1
    for attempt in range(closes):
        before = len(order)
        try:
            session.close()
        except _CloseFailedError:
            assert attempt < max(failures.values())
        else:
            assert attempt == max(failures.values())
        assert order[before:] == [
            name for name in ("delegate", "broker-2", "broker-1") if attempt <= failures[name]
        ]

    settled_order = list(order)
    for _ in range(extra_closes):
        session.close()

    assert order == settled_order
    assert delegate.attempts == delegate_failures + 1
    assert first.attempts == first_failures + 1
    assert second.attempts == second_failures + 1
