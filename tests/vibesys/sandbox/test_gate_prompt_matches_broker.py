"""The gate commands the prompt hands the agent are exactly what the host broker accepts.

Regression: a task that declares ``[benchmark.result] json_argument`` made the broker
demand ``--output-json <path>`` while the prompt named only ``--gate benchmark``, so the
agent's benchmark gate was refused before any Slurm job ran. The agent now names only
the gate, and the broker supplies the task's result argument itself.
"""

from __future__ import annotations

import re
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.prompts import PROMPTS_DIR, render_template
from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    GateKind,
    Gates,
    HostCommandBroker,
    RunRoots,
)

# test-isolation: the client is a single-file CLI that is intentionally absent from the library API.
from vs_sandbox.host_command_client import Stop, execute

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from vs_sim.api import Event

_GATES = ("accuracy", "benchmark")


class _RecordingGates:
    def __init__(self) -> None:
        self.runs: list[tuple[GateKind, tuple[str, ...]]] = []

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        del cwd, cancel
        self.runs.append((kind, tuple(arguments)))
        write(f"gate {kind.value}\n".encode())
        if arguments:
            Path(arguments[-1]).write_text('{"score": 42}', encoding="utf-8")
        return 0


@contextmanager
def _serving(output_argument: str | None, gates: _RecordingGates) -> Iterator[HostCommandBroker]:
    # Unix socket paths are limited to about 100 bytes, so not under pytest's long tmp_path.
    directory = Path(tempfile.mkdtemp(prefix="vs-prompt-"))
    workspace = directory / "workspace"
    workspace.mkdir()
    broker = HostCommandBroker(
        directory / "broker.sock",
        roots=RunRoots((workspace,)),
        gates=Gates(gates, output_argument),
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()
        shutil.rmtree(directory, ignore_errors=True)


def _named_commands(gate_client: str) -> list[list[str]]:
    rendered = render_template(
        "_gate_client.j2",
        template_dir=PROMPTS_DIR / "environments",
        gate_client=gate_client,
        gates=_GATES,
    )
    spans = re.findall(r"`([^`]+)`", str(rendered))
    return [shlex.split(span)[1:] for span in spans if "--gate" in span]


@pytest.mark.parametrize("output_argument", [None, "--output-json", "--vs-output"])
@pytest.mark.parametrize("gate_client", ["vibesys-gate", "vibesys-gpu"])
def test_every_gate_command_the_prompt_names_runs_through_the_broker(
    gate_client: str, output_argument: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _named_commands(gate_client)
    assert [command[-1] for command in commands] == list(_GATES)
    gates = _RecordingGates()

    with _serving(output_argument, gates) as broker:
        monkeypatch.chdir(broker.socket_path.parent / "workspace")
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(broker.socket_path))
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, broker.token)
        statuses = [execute(command, stop=Stop()) for command in commands]

    assert statuses == [0, 0]
    assert [kind for kind, _ in gates.runs] == [GateKind.ACCURACY, GateKind.BENCHMARK]
    benchmark_arguments = gates.runs[1][1]
    assert benchmark_arguments[:1] == (() if output_argument is None else (output_argument,))
