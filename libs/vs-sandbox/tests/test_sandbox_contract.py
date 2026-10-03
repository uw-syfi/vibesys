"""Shared contract for every :class:`~vs_sandbox.execution.Sandbox` implementation.

Parametrized over the real, cheap :class:`~vs_sandbox.local_shell.LocalShellSandbox`
(runs a real subprocess in ``tmp_path``, no container runtime required) and
:class:`~vs_sandbox.api.testing.FakeSandbox` (in-memory, no subprocess). Each
case configures "run a command that succeeds and produces *output*" its own
way (a real shell command for the real sandbox, a scripted result for the
fake) so the same assertions exercise both faithfully.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pytest

from vs_sandbox.api import LocalShellSandbox, Sandbox, SandboxExecutionResult
from vs_sandbox.api.testing import FakeSandbox

if TYPE_CHECKING:
    from pathlib import Path


class _SandboxFactory(Protocol):
    def __call__(self, tmp_path: Path) -> tuple[Sandbox, str]:
        """Return one ready sandbox plus a command that echoes ``"hello"``."""
        ...


def _real_sandbox(tmp_path: Path) -> tuple[Sandbox, str]:
    sandbox = LocalShellSandbox(tmp_path)
    return sandbox, "printf hello"


def _fake_sandbox(tmp_path: Path) -> tuple[Sandbox, str]:
    del tmp_path
    sandbox = FakeSandbox()
    sandbox.script("printf hello", SandboxExecutionResult(output="hello", exit_code=0))
    return sandbox, "printf hello"


_FACTORIES: dict[str, _SandboxFactory] = {
    "real": _real_sandbox,
    "fake": _fake_sandbox,
}


@pytest.mark.parametrize("factory_name", sorted(_FACTORIES))
class TestSandboxContract:
    """Every ``Sandbox`` implementation, probed through the same protocol."""

    def test_id_is_a_nonempty_stable_string(self, factory_name: str, tmp_path: Path) -> None:
        sandbox, _ = _FACTORIES[factory_name](tmp_path)

        assert isinstance(sandbox.id, str)
        assert sandbox.id
        assert sandbox.id == sandbox.id

    def test_execute_returns_the_configured_success(
        self, factory_name: str, tmp_path: Path
    ) -> None:
        sandbox, command = _FACTORIES[factory_name](tmp_path)

        result = sandbox.execute(command)

        assert isinstance(result, SandboxExecutionResult)
        assert result.exit_code == 0
        assert "hello" in result.output
        assert not result.truncated

    def test_execute_rejects_an_empty_command(self, factory_name: str, tmp_path: Path) -> None:
        sandbox, _ = _FACTORIES[factory_name](tmp_path)

        result = sandbox.execute("")

        assert result.exit_code == 1
        assert result.output
