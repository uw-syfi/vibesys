"""The broker clients run in the editor container against a real host broker.

``vibesys-gpu`` is the single-file client the agent runs for GPU commands and
``vibesys-gate`` (the same file, named for gates) for the accuracy and benchmark
gates. The broker is the run's own, serving the editor container over a Unix socket
mounted into it; its ``srun`` is a program that runs the job locally.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.minimal_container.editor import Editor

pytestmark = pytest.mark.minimal_container


def test_the_gate_client_runs_the_planned_accuracy_command(editor: Editor) -> None:
    status, output = editor.gate("accuracy")

    assert status == 0, output
    assert "accuracy ok" in output


def test_the_gate_client_returns_the_benchmark_result_to_the_shared_workspace(
    editor: Editor,
) -> None:
    result = ".vibesys-benchmark-minimal-container.json"

    status, output = editor.gate("benchmark", "--output", result)

    assert status == 0, output
    assert json.loads((editor.request.workspace / result).read_text(encoding="utf-8")) == {
        "score": 42
    }


def test_the_gpu_client_runs_a_command_as_a_job(editor: Editor) -> None:
    status, output = editor.run(f"{shlex.quote(editor.gpu_client)} -- echo from-a-job")

    assert status == 0, output
    assert "from-a-job" in output
