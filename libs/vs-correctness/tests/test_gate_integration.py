from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

from vibesys.loops.gates import run_accuracy_gate


class ShellBackend:
    def execute(self, command: str, timeout: int | None = None) -> SimpleNamespace:
        process = subprocess.run(  # noqa: S603
            ["bash", "-c", command],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return SimpleNamespace(exit_code=process.returncode, output=process.stdout)


def test_vibesys_accuracy_gate_rejects_nonpassing_correctness_report() -> None:
    command = (
        "env PYTHONPATH=libs/vs-correctness/src .venv/bin/python -c "
        "'from vs_correctness import Environment, VerificationReport, gate_exit_code; "
        'r=VerificationReport(seed=1,candidate=Environment(name="c",base_url="http://c"),'
        "results=()); raise SystemExit(gate_exit_code(r))'"
    )
    context = MagicMock()
    context.judge_accuracy_command = command
    context.trusted_input_changes.return_value = []
    context.judge_backend = ShellBackend()

    result = run_accuracy_gate(context, process_id="correctness")

    assert result.executed
    assert not result.passed
    assert result.feedback is not None
