"""Behavior specific to :class:`~vs_sandbox.api.testing.FakeCommandRunner` itself.

The cross-implementation contract lives in ``test_sandbox_contract.py``; this
file covers scripting and call-recording, which only a fake has.
"""

from __future__ import annotations

from vs_sandbox.api import CommandResult
from vs_sandbox.api.testing import FakeCommandRunner


def test_unscripted_command_returns_the_default_result() -> None:
    sandbox = FakeCommandRunner()

    result = sandbox.execute("anything")

    assert result.exit_code == 0
    assert result.output == ""


def test_scripted_command_returns_its_configured_result_every_time() -> None:
    sandbox = FakeCommandRunner()
    sandbox.script("git status", CommandResult(output="clean", exit_code=0))

    first = sandbox.execute("git status")
    second = sandbox.execute("git status")

    assert first.output == "clean"
    assert second.output == "clean"


def test_default_result_is_configurable() -> None:
    sandbox = FakeCommandRunner(default_result=CommandResult(output="boom", exit_code=1))

    result = sandbox.execute("unscripted")

    assert result.exit_code == 1
    assert result.output == "boom"


def test_execute_records_every_call() -> None:
    sandbox = FakeCommandRunner()

    sandbox.execute("echo one", timeout=5)
    sandbox.execute("echo two")

    assert [call.command for call in sandbox.calls] == ["echo one", "echo two"]
    assert sandbox.calls[0].timeout == 5
    assert sandbox.calls[1].timeout is None


def test_invalid_commands_are_not_recorded() -> None:
    sandbox = FakeCommandRunner()

    sandbox.execute("")

    assert sandbox.calls == []


def test_two_instances_have_distinct_ids() -> None:
    assert FakeCommandRunner().id != FakeCommandRunner().id
