"""Name how an xdist worker ended when it dies in the middle of a run.

xdist reports a dead worker as "Not properly terminated" and fails the test it was running,
which says nothing about why. This plugin adds the worker's exit status to the end of the run:
its exit code, or the number of the signal that killed it (11 is a segmentation fault, 9 the
kernel's out-of-memory kill or a timeout's SIGKILL). A worker shares the controller's stderr,
so whatever it printed last (a fault banner, a thread dump) is already in the log.

The status is read in the terminal summary, after xdist's session teardown has reaped every
worker process (``Group.terminate`` waits for each one, bounded by xdist's exit timeout). It is
not read when the worker's connection drops: the pipe reaches end-of-file when the dying process
closes its descriptors, which the kernel does before the process becomes reapable, so a status
read at that point can still say the worker is running (CI run 39158131378).

Together with ``--max-worker-restart=0`` in ``pyproject.toml`` a crash ends the session at once.
Without it ``--dist loadgroup`` keeps waiting for the tests the dead worker still owed
(reproduced with xdist 3.8.0), and a shard that crashed stays silent until its job times out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from xdist.workermanage import WorkerController

_DEAD_WORKERS = pytest.StashKey[list[tuple["WorkerController", object]]]()


def describe_exit(returncode: int | None) -> str:
    """Return the worker's exit status as a sentence fragment."""
    if returncode is None:
        return "still running when the session ended"
    if returncode < 0:
        return f"killed by signal {-returncode}"
    return f"exited with code {returncode}"


class _Process(Protocol):
    def poll(self) -> int | None: ...


def worker_returncode(popen: _Process | None) -> int | None:
    """The worker process's exit status, ``None`` while it runs or when it has no process.

    ``poll`` settles the status of a worker that teardown killed without waiting for it
    (execnet's ``kill``), which ``Popen.returncode`` alone still reports as running.
    """
    return None if popen is None else popen.poll()


def pytest_testnodedown(node: WorkerController, error: object | None) -> None:
    """Remember a worker that died mid-run (a clean shutdown has no error)."""
    if error is not None:
        node.config.stash.setdefault(_DEAD_WORKERS, []).append((node, error))


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Report how each worker that died mid-run ended, once teardown has reaped it."""
    for node, error in config.stash.get(_DEAD_WORKERS, []):
        # execnet's popen gateways hold the worker's process; other gateway kinds have none.
        popen = getattr(node.gateway._io, "popen", None)  # noqa: SLF001  # LW-001001; execnet exposes no public handle on the worker process.
        # > The only alternatives are patching execnet's spawn to keep our own Popen or parsing the
        # > exit status from the log, both hackier than asking the process for its status.
        returncode = worker_returncode(popen)
        terminalreporter.write_line(
            f"worker {node.gateway.id} {describe_exit(returncode)} ({error})", red=True
        )
