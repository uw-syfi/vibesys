"""Name how an xdist worker ended when it dies in the middle of a run.

xdist reports a dead worker as "Not properly terminated" and fails the test it was running,
which says nothing about why. This plugin adds the worker's exit status to the report: its
exit code, or the number of the signal that killed it (11 is a segmentation fault, 9 the
kernel's out-of-memory kill or a timeout's SIGKILL). A worker shares the controller's stderr,
so whatever it printed last (a fault banner, a thread dump) is already in the log just above
this line.

Together with ``--max-worker-restart=0`` in ``pyproject.toml`` a crash ends the session at once.
Without it ``--dist loadgroup`` keeps waiting for the tests the dead worker still owed
(reproduced with xdist 3.8.0), and a shard that crashed stays silent until its job times out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xdist.workermanage import WorkerController


def describe_exit(returncode: int | None) -> str:
    """Return the worker's exit status as a sentence fragment."""
    if returncode is None:
        return "still running after its connection closed"
    if returncode < 0:
        return f"killed by signal {-returncode}"
    return f"exited with code {returncode}"


def pytest_testnodedown(node: WorkerController, error: object | None) -> None:
    """Report how a worker that died mid-run ended (a clean shutdown has no error)."""
    if error is None:
        return
    # execnet's popen gateways hold the worker's process; other gateway kinds have none.
    popen = getattr(node.gateway._io, "popen", None)  # noqa: SLF001  # LW-001001; execnet exposes no public handle on the worker process.
    # > The only alternatives are patching execnet's spawn to keep our own Popen or parsing the
    # > exit status from the log, both hackier than reading one attribute that the pipe's EOF
    # > (what ended the worker) has already settled.
    returncode = None if popen is None else popen.poll()
    node.config.get_terminal_writer().line(
        f"worker {node.gateway.id} {describe_exit(returncode)} ({error})", red=True
    )
