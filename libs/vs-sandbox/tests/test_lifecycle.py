"""Contract tests for backend-independent sandbox lifecycle handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api import (
    BeforeReadyContext,
    SandboxLifecycle,
    SandboxLifecycleError,
    SandboxLifecycleHooks,
    SandboxSession,
    start_sandbox,
    stop_sandbox,
)
from vs_sandbox.api.testing import FakeCommandRunner, FakeLifecycleRunner

if TYPE_CHECKING:
    from vs_sandbox.execution import CommandRunner
from vs_sim.api import DISCARD


@dataclass
class _RecordingHooks(SandboxLifecycleHooks):
    name: str
    events: list[tuple[str, object]]

    def before_ready(self, context: BeforeReadyContext) -> None:
        self.events.append((self.name, context.sandbox))


class _FailingHooks(SandboxLifecycleHooks):
    def before_ready(self, context: BeforeReadyContext) -> None:
        del context
        _failure_message = "setup exploded"
        raise ValueError(_failure_message)


def _sandbox() -> CommandRunner:
    return cast("CommandRunner", object())


def test_base_hooks_are_a_noop() -> None:
    SandboxLifecycle([SandboxLifecycleHooks()]).before_ready(_sandbox())


def test_before_ready_passes_sandbox_to_hooks_in_registration_order() -> None:
    sandbox = _sandbox()
    events: list[tuple[str, object]] = []
    lifecycle = SandboxLifecycle(
        [
            _RecordingHooks("first", events),
            _RecordingHooks("second", events),
        ]
    )

    lifecycle.before_ready(sandbox)

    assert events == [("first", sandbox), ("second", sandbox)]


def test_constructor_snapshots_mutable_hooks_sequence() -> None:
    events: list[tuple[str, object]] = []
    hooks: list[SandboxLifecycleHooks] = [_RecordingHooks("first", events)]
    lifecycle = SandboxLifecycle(hooks)
    hooks.append(_RecordingHooks("late", events))

    lifecycle.before_ready(_sandbox())

    assert [name for name, _ in events] == ["first"]
    assert lifecycle.hooks == (hooks[0],)


def test_failure_names_hooks_provider_preserves_cause_and_stops_dispatch() -> None:
    events: list[tuple[str, object]] = []
    lifecycle = SandboxLifecycle(
        [
            _FailingHooks(),
            _RecordingHooks("not-run", events),
        ]
    )

    with pytest.raises(
        SandboxLifecycleError,
        match=r"before_ready hook in _FailingHooks failed: setup exploded",
    ) as error:
        lifecycle.before_ready(_sandbox())

    assert isinstance(error.value.__cause__, ValueError)
    assert events == []


def test_owned_session_starts_and_stops_sandbox_once() -> None:
    sandbox = FakeLifecycleRunner()

    session = SandboxSession.start(sandbox, {"location": "container"}, output=DISCARD)

    assert session.sandbox is sandbox
    assert session.view == {"location": "container"}
    assert sandbox.start_count == 1
    assert sandbox.stop_count == 0

    session.close()
    session.close()

    assert sandbox.stop_count == 1


def test_owned_session_context_exit_stops_after_an_error() -> None:
    sandbox = FakeLifecycleRunner()
    failure_message = "failed inside session"

    with (
        pytest.raises(ValueError, match=failure_message),
        SandboxSession.start(sandbox, "container", output=DISCARD),
    ):
        raise ValueError(failure_message)

    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1


def test_borrowed_session_never_stops_sandbox() -> None:
    sandbox = FakeLifecycleRunner()

    with SandboxSession.borrowed(sandbox, "host") as session:
        assert session.view == "host"

    session.close()
    assert sandbox.start_count == 0
    assert sandbox.stop_count == 0


def test_start_sandbox_rejects_sandbox_without_lifecycle() -> None:
    sandbox = FakeCommandRunner()

    with pytest.raises(TypeError, match="FakeCommandRunner has no execution environment to start"):
        start_sandbox(sandbox, output=DISCARD)


def test_stop_sandbox_ignores_sandbox_without_lifecycle() -> None:
    stop_sandbox(FakeCommandRunner())


@given(failures=st.integers(0, 4), extra_closes=st.integers(0, 2))
def test_an_owned_session_stops_again_after_a_failed_stop_until_one_returns(
    failures: int, extra_closes: int
) -> None:
    """Regression: a stop that raised latched the session closed, so a retry stopped nothing."""
    sandbox = FakeLifecycleRunner()
    session = SandboxSession.start(sandbox, "container", output=DISCARD)
    sandbox.stop_error = RuntimeError("docker rm -f was killed")
    for _ in range(failures):
        with pytest.raises(RuntimeError, match="killed"):
            session.close()
    sandbox.stop_error = None
    for _ in range(1 + extra_closes):
        session.close()

    assert sandbox.stop_count == failures + 1
