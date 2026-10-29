"""Behavior specific to :class:`~vs_sandbox.api.testing.FakeCommandRunner` itself.

The cross-implementation contract lives in ``test_sandbox_contract.py``; this
file covers scripting and call-recording, which only a fake has.
"""

from __future__ import annotations

import pytest

from vs_sandbox.api import CommandResult
from vs_sandbox.api.testing import FakeCommandRunner
from vs_sim.api import DISCARD
from vs_sim.api.testing import SimThreads


def test_unscripted_command_returns_the_default_result() -> None:
    sandbox = FakeCommandRunner()

    result = sandbox.execute("anything", output=DISCARD)

    assert result.exit_code == 0
    assert result.output == ""


def test_scripted_command_returns_its_configured_result_every_time() -> None:
    sandbox = FakeCommandRunner()
    sandbox.script("git status", CommandResult(output="clean", exit_code=0))

    first = sandbox.execute("git status", output=DISCARD)
    second = sandbox.execute("git status", output=DISCARD)

    assert first.output == "clean"
    assert second.output == "clean"


def test_default_result_is_configurable() -> None:
    sandbox = FakeCommandRunner(default_result=CommandResult(output="boom", exit_code=1))

    result = sandbox.execute("unscripted", output=DISCARD)

    assert result.exit_code == 1
    assert result.output == "boom"


def test_truncated_result_requires_a_prebounded_diagnostic_for_each_retained_view() -> None:
    with pytest.raises(ValueError, match="requires diagnostic_output"):
        CommandResult(output="OKEN=sandbox-secret", exit_code=1, truncated=True)

    with pytest.raises(ValueError, match="requires diagnostic_stdout"):
        CommandResult(
            output="stdout fragment",
            stdout="stdout fragment",
            exit_code=1,
            truncated=True,
            diagnostic_output="safe aggregate",
        )

    with pytest.raises(ValueError, match="requires diagnostic_stderr"):
        CommandResult(
            output="stderr fragment",
            stderr="stderr fragment",
            exit_code=1,
            truncated=True,
            diagnostic_output="safe aggregate",
            diagnostic_stdout="",
        )


def test_truncated_result_derives_empty_diagnostics_for_unrepresented_streams() -> None:
    empty = CommandResult(output="", exit_code=1, truncated=True)
    aggregate_only = CommandResult(
        output="retained aggregate",
        exit_code=1,
        truncated=True,
        diagnostic_output="safe aggregate",
    )

    assert (empty.diagnostic_output, empty.diagnostic_stdout, empty.diagnostic_stderr) == (
        "",
        "",
        "",
    )
    assert aggregate_only.diagnostic_output == "safe aggregate"
    assert aggregate_only.diagnostic_stdout == aggregate_only.diagnostic_stderr == ""


def test_truncated_result_preserves_explicitly_empty_diagnostics() -> None:
    result = CommandResult(
        output="OKEN=sandbox-secret",
        exit_code=1,
        truncated=True,
        diagnostic_output="",
        diagnostic_stdout="",
        diagnostic_stderr="",
    )

    assert result.diagnostic_output == ""


@pytest.mark.parametrize("split_at", range(1, len("API_TOKEN")))
def test_failed_process_keeps_raw_output_but_bounds_a_pre_redacted_diagnostic(
    split_at: int,
) -> None:
    """The raw command contract stays intact while its UI-safe view cannot leak a split key."""
    limit = 100
    credential = "API_TOKEN=stream-secret"
    # A failed stderr-only result has 81 payload characters after its 19-character
    # marker. Place that retained tail exactly `split_at` characters into the key.
    suffix = "z" * (81 + split_at - len(credential) - 1)
    stderr = "p" * 32 + credential + " " + suffix
    sandbox = FakeCommandRunner(max_output_chars=limit)
    sandbox.script_process("fail", stderr=stderr, returncode=1)

    result = sandbox.execute("fail", output=DISCARD)

    assert "stream-secret" in result.stderr
    assert "API_TOKEN" not in result.stderr
    assert "stream-secret" not in result.diagnostic_stderr
    assert "stream-secret" not in result.diagnostic_output
    assert len(result.output) <= limit
    assert len(result.diagnostic_output) <= limit


def test_execute_records_every_call() -> None:
    sandbox = FakeCommandRunner()

    sandbox.execute("echo one", timeout=5, output=DISCARD)
    sandbox.execute("echo two", output=DISCARD)

    assert [call.command for call in sandbox.calls] == ["echo one", "echo two"]
    assert sandbox.calls[0].timeout == 5
    assert sandbox.calls[1].timeout is None


def test_invalid_commands_are_not_recorded() -> None:
    sandbox = FakeCommandRunner()

    sandbox.execute("", output=DISCARD)

    assert sandbox.calls == []


def test_two_instances_have_distinct_ids() -> None:
    assert FakeCommandRunner().id != FakeCommandRunner().id


class _ObservedCancel:
    """A cancel event that records whether the fake was hanging when waited on.

    The fake waits on the event only once its command is blocked, so the
    recorded value is the fake's state at the moment a real cancel could land.
    A single-threaded stand-in for the interleaving "cancel arrives while the
    command is running".
    """

    def __init__(self, runner: FakeCommandRunner) -> None:
        self._runner = runner
        self._set = False
        self.hanging_when_waited_on: bool | None = None

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def clear(self) -> None:
        self._set = False

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.hanging_when_waited_on = self._runner.hanging
        self.set()
        return True


def test_a_hang_reports_it_is_running_before_it_waits_for_a_cancel() -> None:
    sandbox = FakeCommandRunner()
    sandbox.script_hang("hang", stdout="partial-out", stderr="partial-err")
    cancel = _ObservedCancel(sandbox)

    result = sandbox.execute("hang", cancel=cancel, output=DISCARD)

    assert cancel.hanging_when_waited_on is True
    assert result.cancelled
    assert result.stdout == "partial-out"
    sandbox.wait_until_hanging()


def test_a_cancel_set_before_the_call_never_starts_the_hang() -> None:
    sandbox = FakeCommandRunner()
    sandbox.script_hang("hang", stdout="partial-out", stderr="partial-err")
    cancel = SimThreads().event()
    cancel.set()

    result = sandbox.execute("hang", cancel=cancel, output=DISCARD)

    assert result.cancelled
    assert result.stdout == ""
    assert not sandbox.hanging
