"""Faults at the cluster boundary: a wrapper over any Slurm connector command.

Run as ``python -m vs_faults.connector PLAN STATE -- INNER...``. It speaks
the versioned connector protocol (one JSON request on stdin, one JSON response
on stdout), forwards each request to ``INNER`` (the real connector or the Fake
cluster), and on the calls ``PLAN`` schedules answers instead as a failing
cluster does. Connector processes run concurrently, so call counts, phantom
jobs, and the fault log live under ``STATE`` behind a file lock.

Calls are counted per :class:`~vs_faults.plan.ClusterOperation`. A killed job
never runs: its sbatch is answered with a fresh job id, ``squeue`` no longer
lists it, and ``sacct`` reports how it died.
"""

from __future__ import annotations

import fcntl
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vs_faults.plan import Boundary, ClusterFault, ClusterOperation, FaultPlan

if TYPE_CHECKING:
    from collections.abc import Sequence

_STATE_FILE = "state.json"
_LOCK_FILE = "lock"
#: The log of every fault the wrapper injected, one JSON object per line.
INJECTED_FILE = "injected.jsonl"
_PHANTOM_FIRST_JOB = 90000
_KILLED_STATES = ("OUT_OF_MEMORY 0:125", "PREEMPTED 0:0", "NODE_FAIL 0:0", "FAILED 137:9")
_ERRORS: dict[ClusterOperation, tuple[int, str]] = {
    ClusterOperation.SBATCH: (
        1,
        "sbatch: error: Batch job submission failed: Socket timed out on send/recv operation\n",
    ),
    ClusterOperation.SQUEUE: (
        1,
        "squeue: error: slurm_load_jobs error: Socket timed out on send/recv operation\n",
    ),
    ClusterOperation.SACCT: (1, "sacct: error: Problem talking to the database\n"),
    ClusterOperation.SCANCEL: (1, "scancel: error: Unable to contact slurm controller\n"),
    ClusterOperation.EXEC: (1, "bash: line 1: Resource temporarily unavailable\n"),
    ClusterOperation.TRANSFER: (12, "rsync error: error in rsync protocol data stream (code 12)\n"),
}
_WRONG: dict[ClusterOperation, str] = {
    ClusterOperation.SQUEUE: "slurm_load_jobs: garbled\x00 RUNNING?\n",
    ClusterOperation.SACCT: "BOGUS_STATE ?:?\n",
}


def classify(request: dict[str, object]) -> tuple[ClusterOperation, list[str]]:
    """Return a connector request's operation and its command tokens."""
    if request.get("operation") != "exec":
        return ClusterOperation.TRANSFER, []
    try:
        tokens = shlex.split(str(request.get("command", "")))
    except ValueError:
        return ClusterOperation.EXEC, []
    if "sbatch" in tokens:
        return ClusterOperation.SBATCH, tokens
    head = tokens[:1]
    for operation in (ClusterOperation.SQUEUE, ClusterOperation.SACCT, ClusterOperation.SCANCEL):
        if head == [operation.value]:
            return operation, tokens
    return ClusterOperation.EXEC, tokens


def _response(returncode: int, stdout: str = "", stderr: str = "") -> dict[str, object]:
    return {"version": 1, "returncode": returncode, "stdout": stdout, "stderr": stderr}


def _forward(inner: Sequence[str], request: dict[str, object]) -> dict[str, object]:
    # lint-waiver: LW-150006 [S603]; the wrapper runs the connector command
    # > the test configured, exactly as the production connector transport does.
    completed = subprocess.run(  # noqa: S603
        list(inner), input=json.dumps(request), capture_output=True, text=True, check=True
    )
    response = json.loads(completed.stdout)
    if not isinstance(response, dict):
        message = f"connector answered with a non-object: {completed.stdout[:200]!r}"
        raise TypeError(message)
    return response


def handle(
    plan: FaultPlan, state_dir: Path, inner: Sequence[str], request: dict[str, object]
) -> dict[str, object]:
    """Answer one connector request, injecting the fault ``plan`` schedules for it."""
    operation, tokens = classify(request)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / _LOCK_FILE).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state_dir / _STATE_FILE
        state = json.loads(path.read_text()) if path.exists() else {"counts": {}, "killed": {}}
        counts: dict[str, int] = state["counts"]
        killed: dict[str, str] = state["killed"]
        counts[operation.value] = counts.get(operation.value, 0) + 1
        ordinal = counts[operation.value]
        rule = plan.match(Boundary.CLUSTER, operation.value, ordinal)
        fault = rule.fault if rule is not None else None
        phantom = next((token for token in tokens if token in killed), None)
        response: dict[str, object] | None = None
        if fault is ClusterFault.SSH_DOWN:
            response = _response(255, stderr="ssh: connect to host login port 22: timed out\n")
        elif fault is ClusterFault.COMMAND_ERROR:
            response = _response(_ERRORS[operation][0], stderr=_ERRORS[operation][1])
        elif fault is ClusterFault.KILLED and operation is ClusterOperation.SBATCH:
            job = str(_PHANTOM_FIRST_JOB + len(killed))
            killed[job] = plan.rng("killed", ordinal).choice(_KILLED_STATES)
            response = _response(0, f"Submitted batch job {job}\n")
        elif phantom is not None:
            stdout = f"{killed[phantom]}\n" if operation is ClusterOperation.SACCT else ""
            response = _response(0, stdout)
        if fault is not None:
            with (state_dir / INJECTED_FILE).open("a", encoding="utf-8") as log:
                entry = {"operation": operation.value, "at": ordinal, "fault": fault.value}
                log.write(json.dumps(entry) + "\n")
        path.write_text(json.dumps(state))
    if response is not None:
        return response
    response = _forward(inner, request)
    if fault is ClusterFault.WRONG_STATE and operation in _WRONG:
        response = {**response, "stdout": _WRONG[operation]}
    return response


def injected_faults(state_dir: Path) -> list[dict[str, object]]:
    """Return every fault the wrapper injected under ``state_dir``, in order."""
    path = state_dir / INJECTED_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def connector_command(plan_path: Path, state_dir: Path, inner: Sequence[str]) -> list[str]:
    """Return the connector command that wraps ``inner`` with the plan at ``plan_path``."""
    return [
        sys.executable,
        "-m",
        "vs_faults.connector",
        str(plan_path),
        str(state_dir),
        "--",
        *inner,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Answer the one request on stdin."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    separator = arguments.index("--")
    plan = FaultPlan.load(Path(arguments[0]))
    response = handle(plan, Path(arguments[1]), arguments[separator + 1 :], json.load(sys.stdin))
    sys.stdout.write(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
