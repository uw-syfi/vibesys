"""The host-side runners behind the broker's ``gate`` operation.

A gate is the framework's trusted accuracy or benchmark command. The agent's
container cannot run it, because the Slurm tools and credentials are on the
host, so the host runs it for the container:

* :class:`SlurmCommandGateRunner` for the ``slurm`` environment runs the planned
  gate through ``vs_sandbox.slurm_command``, which stages the workspace to the
  remote cluster, runs the job there, and copies the result back.
* :class:`SrunGateRunner` for the ``slurm-gpu`` environment runs the planned
  command in a new local Slurm allocation, unconfined: it is the framework's
  trusted command, not the agent's.

Both stop when asked to: cancelling a gate cancels its Slurm job.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from vs_sandbox.slurm_gpu import GpuCommand, GpuJobRequest, GpuLauncher

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_sandbox.host_command_broker import GateKind
    from vs_sim.api import Event

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
    ) -> None:
        """Bind the evaluation plan, the host environment, and the wrapper program."""
        self._plan_path = plan_path
        self._env = dict(env)
        self._wrapper = tuple(wrapper)

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
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-610010 [S603]; run the fixed wrapper argv with validated gate arguments, without a shell.
            # > The argv is the wrapper this module names plus arguments the broker
            # > validated against the plan; a shell would only add quoting risk.
            argv,
            cwd=cwd,
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Keep terminal signals aimed at the caller away from the wrapper,
            # which cancels its job when this runner asks it to.
            process_group=0,
        )
        finished = threading.Event()
        watcher = threading.Thread(
            target=_stop_when_asked,
            args=(process, cancel, finished),
            name="vibesys-gate-cancel",
            daemon=True,
        )
        watcher.start()
        try:
            stdout = process.stdout
            if stdout is not None:
                while chunk := os.read(stdout.fileno(), _READ_BYTES):
                    write(chunk)
            return process.wait()
        finally:
            finished.set()
            watcher.join()


def _stop_when_asked(
    process: subprocess.Popen[bytes], cancel: Event, finished: threading.Event
) -> None:
    while not finished.is_set():
        if cancel.wait(0.2):
            break
    else:
        return
    process.terminate()
    try:
        process.wait(_CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()


class SrunGateRunner:
    """Run a gate's planned command in a new local Slurm allocation, unconfined.

    *planned* maps each gate the run offers to its fixed argv; the arguments
    the broker validated are appended. *request* sizes every gate's job.
    """

    def __init__(
        self,
        launcher: GpuLauncher,
        request: GpuJobRequest,
        planned: Mapping[GateKind, Sequence[str]],
        *,
        env: Mapping[str, str],
    ) -> None:
        """Bind the launcher, the gate allocation, the planned commands, and the host environment.

        The host's own Slurm variables (``SLURM_*``) are dropped: a gate is always
        a new allocation. A host started inside ``salloc`` or a batch job would
        otherwise make ``srun`` run the gate as a step of that allocation, or fail
        on one that has ended.
        """
        self._launcher = launcher
        self._request = request
        self._planned = {kind: tuple(argv) for kind, argv in planned.items()}
        self._env = {key: value for key, value in env.items() if not key.startswith("SLURM_")}

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        """Run the planned *kind* gate from *cwd*; a gate the run did not plan exits 2."""
        planned = self._planned.get(kind)
        if planned is None:
            write(f"vibesys-gpu: the {kind.value} gate is not configured\n".encode())
            return 2
        return self._launcher.run(
            self._request,
            GpuCommand(argv=(*planned, *arguments), cwd=cwd, env=self._env),
            write=write,
            cancel=cancel,
        )


__all__ = ["DEFAULT_WRAPPER", "SlurmCommandGateRunner", "SrunGateRunner"]
