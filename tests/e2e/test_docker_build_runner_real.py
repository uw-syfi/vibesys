"""Real-process contract checks for bounded Docker command execution."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vs_agent.api.images import (
    DOCKER_OUTPUT_TAIL_CHARACTERS,
    SubprocessDockerBuildRunner,
)
from vs_sim.api import PipedProcess, PipedProcessLauncher, SubprocessPipedProcessLauncher

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
from vs_sim.api import DISCARD

pytestmark = pytest.mark.real_contract


@dataclass(frozen=True)
class _ReadyLauncher:
    """Delay the runner's timeout until the real descendant owns both output pipes."""

    delegate: PipedProcessLauncher
    ready_fifo: Path

    def start(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        merge_stderr: bool = False,
    ) -> PipedProcess:
        process = self.delegate.start(argv, cwd=cwd, env=env, merge_stderr=merge_stderr)
        assert self.ready_fifo.read_text(encoding="utf-8") == "ready"
        return process


def test_real_runner_drains_large_multiline_and_unterminated_streams_into_bounded_tails(
    tmp_path: Path,
) -> None:
    repetitions = DOCKER_OUTPUT_TAIL_CHARACTERS
    stdout_line = "stdout-line\n"
    stdout_end = "stdout-end\n"
    stderr_character = "e"
    stderr_end = "stderr-end"
    program = (
        "import sys; "
        f"sys.stdout.write({stdout_line!r} * {repetitions} + {stdout_end!r}); "
        "sys.stdout.flush(); "
        f"sys.stderr.write({stderr_character!r} * {repetitions * 2} + {stderr_end!r}); "
        "sys.stderr.flush(); "
        "raise SystemExit(23)"
    )

    result = SubprocessDockerBuildRunner().run(
        (sys.executable, "-c", program),
        cwd=tmp_path,
        timeout=30,
        output=DISCARD,
    )

    assert result.returncode == 23
    assert len(result.stdout) == DOCKER_OUTPUT_TAIL_CHARACTERS
    assert len(result.stderr) == DOCKER_OUTPUT_TAIL_CHARACTERS
    assert result.stdout.endswith(stdout_end)
    assert result.stderr.endswith(stderr_end)
    assert (
        result.stdout_omitted_characters
        == len(stdout_line) * repetitions + len(stdout_end) - DOCKER_OUTPUT_TAIL_CHARACTERS
    )
    assert (
        result.stderr_omitted_characters
        == repetitions * 2 + len(stderr_end) - DOCKER_OUTPUT_TAIL_CHARACTERS
    )


def test_timeout_kills_descendant_that_inherits_both_output_pipes(tmp_path: Path) -> None:
    ready_fifo = tmp_path / "descendant-ready"
    os.mkfifo(ready_fifo)
    descendant = (
        "import pathlib, sys, time; "
        "path = pathlib.Path(sys.argv[1]); "
        "path.write_text('ready', encoding='utf-8'); "
        "time.sleep(600)"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}, {str(ready_fifo)!r}]); "
        "time.sleep(600)"
    )
    launcher = _ReadyLauncher(SubprocessPipedProcessLauncher(), ready_fifo)

    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessDockerBuildRunner(processes=launcher).run(
            (sys.executable, "-c", parent),
            cwd=tmp_path,
            timeout=0,
            output=DISCARD,
        )
