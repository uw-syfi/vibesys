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
)

if TYPE_CHECKING:
    from vs_sandbox.execution import Sandbox


@dataclass
class _RecordingHooks(SandboxLifecycleHooks):
    name: str
    events: list[tuple[str, object]]

    def before_ready(self, context: BeforeReadyContext) -> None:
        self.events.append((self.name, context.sandbox))


class _FailingHooks(SandboxLifecycleHooks):
    def before_ready(self, context: BeforeReadyContext) -> None:  # noqa: ARG002
        raise ValueError("setup exploded")  # noqa: TRY003


def _sandbox() -> Sandbox:
    return cast("Sandbox", object())


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
