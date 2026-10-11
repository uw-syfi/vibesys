"""The host-side runners behind the broker's ``gate`` operation.

A gate is the framework's trusted accuracy or benchmark command. The agent's
container cannot run it, because the Slurm tools and credentials are on the
host, so the host runs it for the container:

* :class:`SlurmCommandGateRunner` for the ``slurm`` environment runs the planned
  gate through ``vs_sandbox.slurm_command``, which stages the workspace to the
  remote cluster, runs the job there, and copies the result back.
Cancelling a gate cancels its Slurm job.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from vs_sim.api import (
    MergedOutputProcess,
    MergedOutputProcessLauncher,
    OsThreads,
    SubprocessMergedOutputProcessLauncher,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_sandbox.host_command_broker import GateKind
    from vs_sim.api import Event, Threads

#: The program that runs one planned gate against a Slurm cluster.
DEFAULT_WRAPPER = (sys.executable, "-m", "vs_sandbox.slurm_command")
_READ_BYTES = 65_536
# ``slurm_command`` cancels its submitted job on SIGTERM, which takes a few
# round trips to the scheduler; give it time before it is killed.
_CANCEL_GRACE_SECONDS = 120


class SlurmCommandGateRunner:
    """Run a gate through ``vs_sandbox.slurm_command`` on the planned evaluation.

    *plan_path* is the evaluation plan the run wrote. *wrapper* is the program
    that runs one planned gate and takes ``--plan PLAN KIND ARGUMENT...``;
    tests substitute a fake that stands in for the cluster.
    """

    def __init__(
        self,
        plan_path: Path,
        *,
        env: Mapping[str, str],
        wrapper: Sequence[str] = DEFAULT_WRAPPER,
        processes: MergedOutputProcessLauncher | None = None,
        threads: Threads | None = None,
    ) -> None:
        """Bind the evaluation plan, the host environment, and the wrapper program."""
        self._plan_path = plan_path
        self._env = dict(env)
        self._wrapper = tuple(wrapper)
        self._processes = processes or SubprocessMergedOutputProcessLauncher()
        self._threads = threads or OsThreads()

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        """Run the gate from *cwd*; setting *cancel* sends the wrapper ``SIGTERM``."""
        argv = (*self._wrapper, "--plan", str(self._plan_path), kind.value, *arguments)
        process = self._processes.start(
            argv,
            cwd=cwd,
            env=self._env,
        )
        finished = self._threads.event()
        watcher = self._threads.spawn(
            lambda: _stop_when_asked(process, cancel, finished),
            name="vibesys-gate-cancel",
            daemon=True,
        )
        try:
            while chunk := process.read(_READ_BYTES):
                write(chunk)
            return process.wait()
        finally:
            finished.set()
            watcher.join()


def _stop_when_asked(process: MergedOutputProcess, cancel: Event, finished: Event) -> None:
    while not finished.is_set():
        if cancel.wait(0.2):
            break
    else:
        return
    if finished.is_set():
        return
    process.terminate_group()
    if not finished.wait(_CANCEL_GRACE_SECONDS):
        process.kill_group()


__all__ = ["DEFAULT_WRAPPER", "SlurmCommandGateRunner"]
