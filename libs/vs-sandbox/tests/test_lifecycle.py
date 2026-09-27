"""Contract tests for backend-independent sandbox lifecycle handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from vs_sandbox.api import (
    BeforeReadyContext,
    SandboxLifecycle,
    SandboxLifecycleError,
    SandboxLifecycleHooks,
    SandboxSession,
    start_sandbox,
    stop_sandbox,
)
from vs_sandbox.api.testing import FakeSandbox

if TYPE_CHECKING:
    from vs_sandbox.execution import Sandbox


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


def _sandbox() -> Sandbox:
    return cast("Sandbox", object())


class _FakeLifecycleSandbox(FakeSandbox):
    """In-memory lifecycle capability for the public session contract."""

    def __init__(self) -> None:
        super().__init__()
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1


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
    sandbox = _FakeLifecycleSandbox()

    session = SandboxSession.start(sandbox, {"location": "container"})

    assert session.sandbox is sandbox
    assert session.view == {"location": "container"}
    assert sandbox.start_count == 1
    assert sandbox.stop_count == 0

    session.close()
    session.close()

    assert sandbox.stop_count == 1


def test_owned_session_context_exit_stops_after_an_error() -> None:
    sandbox = _FakeLifecycleSandbox()
    failure_message = "failed inside session"

    with (
        pytest.raises(ValueError, match=failure_message),
        SandboxSession.start(sandbox, "container"),
    ):
        raise ValueError(failure_message)

    assert sandbox.start_count == 1
    assert sandbox.stop_count == 1


def test_borrowed_session_never_stops_sandbox() -> None:
    sandbox = _FakeLifecycleSandbox()

    with SandboxSession.borrowed(sandbox, "host") as session:
        assert session.view == "host"

    session.close()
    assert sandbox.start_count == 0
    assert sandbox.stop_count == 0


def test_start_sandbox_rejects_sandbox_without_lifecycle() -> None:
    sandbox = FakeSandbox()

    with pytest.raises(TypeError, match="FakeSandbox has no execution environment to start"):
        start_sandbox(sandbox)


def test_stop_sandbox_ignores_sandbox_without_lifecycle() -> None:
    stop_sandbox(FakeSandbox())
