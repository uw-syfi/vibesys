"""Executable Fake Slurm cluster speaking the versioned connector protocol.

Configure a connector transport whose command is
``python -m vs_slurm.fake_connector STATE_DIR`` and whose ``sbatch_command``
is the default ``sbatch``. The cluster has two modes, chosen by files in
``STATE_DIR``:

- Pending (default): every submitted job is ``JOB_ID`` and stays ``PENDING``
  until ``scancel`` cancels it, so a test can observe whether a caller that
  stops early cancels its job. Other commands, uploads and downloads touch
  nothing, and content-cache probes report the object as present, so staging
  never uploads.
- Executing (``run`` exists, see :func:`executing_cluster`): the "remote" is
  the local filesystem. ``remote_workspace_root`` must be a local absolute
  directory; every ``exec`` runs under ``bash``, transfers copy files, and
  ``sbatch`` runs the job script to completion before it returns, so the job
  is ``COMPLETED`` or ``FAILED`` at its first poll and nobody waits. Job ids
  are unique. While ``hold`` exists, a new job is not run and stays
  ``PENDING`` until ``scancel``.

Other files in ``STATE_DIR``:

- ``requests.jsonl``: every request, one JSON object per line, in order.
- ``submitted``: if this path exists (typically a FIFO the test reads), the
  connector writes the job id to it when a job is left pending. A FIFO lets a
  test block until the job exists without polling.
- ``cancelled``: created when ``scancel`` cancels a job.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

JOB_ID = "4242"
REQUESTS_FILE = "requests.jsonl"
SUBMITTED_FILE = "submitted"
CANCELLED_FILE = "cancelled"
RUN_FILE = "run"
HOLD_FILE = "hold"
_JOBS_DIRECTORY = "jobs"
_PENDING = "PENDING"
_FIRST_EXECUTING_JOB = 5000


def executing_cluster(state: Path) -> Path:
    """Switch ``state`` to the executing mode and return it."""
    state.mkdir(parents=True, exist_ok=True)
    (state / RUN_FILE).touch()
    return state


def _pending_exec(state: Path, command: str) -> str:
    tokens = shlex.split(command)
    if tokens[:3] == ["if", "[", "-f"]:
        return "READY"
    if tokens[:1] == ["scancel"]:
        (state / CANCELLED_FILE).touch()
        return ""
    if tokens[:1] == ["squeue"]:
        return "" if (state / CANCELLED_FILE).exists() else "PENDING\n"
    if tokens[:1] == ["sacct"]:
        return "CANCELLED 0:0\n"
    if "sbatch" in tokens:
        _announce(state, JOB_ID)
        return f"Submitted batch job {JOB_ID}\n"
    return ""


def _announce(state: Path, job_id: str) -> None:
    submitted = state / SUBMITTED_FILE
    if submitted.exists():
        with submitted.open("w", encoding="utf-8") as handle:
            handle.write(job_id)


def _allocate_job(state: Path) -> tuple[str, Path]:
    """Reserve a unique job id; connector processes run concurrently."""
    jobs = state / _JOBS_DIRECTORY
    jobs.mkdir(exist_ok=True)
    number = _FIRST_EXECUTING_JOB
    while True:
        record = jobs / str(number)
        try:
            os.close(os.open(record, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            number += 1
            continue
        return str(number), record


def _submit(state: Path, tokens: list[str]) -> str:
    """Run one ``cd BASE && sbatch --output=LOG ... SCRIPT`` to completion, or hold it."""
    job_id, record = _allocate_job(state)
    if (state / HOLD_FILE).exists():
        record.write_text(_PENDING, encoding="utf-8")
        _announce(state, job_id)
        return f"Submitted batch job {job_id}\n"
    start = tokens.index("sbatch")
    directory = Path(tokens[tokens.index("cd") + 1]) if "cd" in tokens[:start] else Path.cwd()
    output = next(
        Path(token.split("=", 1)[1]) for token in tokens[start:] if token.startswith("--output=")
    )
    script = Path(tokens[-1])
    with output.open("w", encoding="utf-8") as log:
        # lint-waiver: LW-140001 [S603]; the Fake cluster runs the exact
        # > production-generated job script; reading the script instead of
        # > running it would not produce the result files callers download.
        completed = subprocess.run(  # noqa: S603
            ("/bin/bash", str(script)),
            cwd=directory,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    state_line = (
        "COMPLETED 0:0" if completed.returncode == 0 else f"FAILED {completed.returncode}:0"
    )
    record.write_text(state_line, encoding="utf-8")
    return f"Submitted batch job {job_id}\n"


def _job_state(state: Path, job_id: str) -> str:
    record = state / _JOBS_DIRECTORY / job_id
    return record.read_text(encoding="utf-8") if record.is_file() else ""


def _executing_exec(state: Path, command: str) -> tuple[int, str, str]:
    tokens = shlex.split(command)
    if "sbatch" in tokens:
        return 0, _submit(state, tokens), ""
    if tokens[:1] == ["squeue"]:
        return 0, ("PENDING\n" if _job_state(state, tokens[3]) == _PENDING else ""), ""
    if tokens[:1] == ["sacct"]:
        return 0, f"{_job_state(state, tokens[4])}\n", ""
    if tokens[:1] == ["scancel"]:
        record = state / _JOBS_DIRECTORY / tokens[1]
        if record.is_file() and record.read_text(encoding="utf-8") == _PENDING:
            record.write_text("CANCELLED 0:0", encoding="utf-8")
        (state / CANCELLED_FILE).touch()
        return 0, "", ""
    # lint-waiver: LW-140002 [S603]; the executing cluster's remote shell is
    # > the local one, so production staging programs run unchanged; parsing
    # > them instead would fake their semantics.
    completed = subprocess.run(  # noqa: S603
        ("/bin/bash", "-c", command), check=False, capture_output=True, text=True
    )
    return completed.returncode, completed.stdout, completed.stderr


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _transfer(request: dict[str, object]) -> tuple[int, str]:
    operation = request["operation"]
    if operation == "put":
        _copy(Path(str(request["local_path"])), Path(str(request["remote_path"])))
        return 0, ""
    if operation == "sync_to":
        source = Path(str(request["local_dir"]))
        destination = Path(str(request["remote_dir"]))
        if bool(request["delete"]) and destination.exists():
            shutil.rmtree(destination)
        excludes = [str(item).strip("/") for item in request.get("excludes", [])]  # ty: ignore[not-iterable]
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*excludes),
        )
        return 0, ""
    remote = Path(str(request["remote_path"]))
    local = Path(str(request["local_path"]))
    if not remote.exists():
        return 1, f"{remote}: No such file or directory"
    if request["kind"] == "tree":
        shutil.copytree(remote, local, symlinks=True, dirs_exist_ok=True)
    else:
        _copy(remote, local)
    return 0, ""


def _executing(state: Path, request: dict[str, object]) -> dict[str, object]:
    if request["operation"] == "exec":
        returncode, stdout, stderr = _executing_exec(state, str(request["command"]))
    else:
        returncode, stderr = _transfer(request)
        stdout = ""
    return {"version": 1, "returncode": returncode, "stdout": stdout, "stderr": stderr}


def handle(state: Path, request: dict[str, object]) -> dict[str, object]:
    """Record one connector request and return its protocol response."""
    with (state / REQUESTS_FILE).open("a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    if (state / RUN_FILE).exists():
        return _executing(state, request)
    stdout = _pending_exec(state, str(request["command"])) if request["operation"] == "exec" else ""
    return {"version": 1, "returncode": 0, "stdout": stdout, "stderr": ""}


def recorded_commands(state: Path) -> list[str]:
    """Return every ``exec`` command the connector received, in order."""
    path = state / REQUESTS_FILE
    if not path.exists():
        return []
    requests = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [str(item["command"]) for item in requests if item["operation"] == "exec"]


def main() -> int:
    """Answer the one request on stdin."""
    state = Path(sys.argv[1])
    sys.stdout.write(json.dumps(handle(state, json.loads(sys.stdin.read()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
