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

The same cluster also stands in for the SSH transport's programs, so a test
can route every call through the host-side broker as production does: set
``ssh_command = [python, -m, vs_slurm.fake_connector, STATE_DIR, ssh]`` and
``rsync_command = [python, -m, vs_slurm.fake_connector, STATE_DIR, rsync]``.
``ssh ... -- HOST COMMAND`` answers ``COMMAND`` as an ``exec`` request. rsync
transfers are recorded and do nothing, so the SSH stand-in supports only the
pending mode.

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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from .runner import SlurmJobStatus

JOB_ID = "4242"
REQUESTS_FILE = "requests.jsonl"
SUBMITTED_FILE = "submitted"
CANCELLED_FILE = "cancelled"
RUN_FILE = "run"
HOLD_FILE = "hold"
_JOBS_DIRECTORY = "jobs"
_PENDING = "PENDING"
_FIRST_EXECUTING_JOB = 5000
_STATES_REQUIRED = "states must contain at least one scheduler observation"
_STDIN_REQUIRED = "connector stdin must contain one request"
_LOST_REPLY = "submit reply lost after scheduler acceptance"
_UNKNOWN_FAULT = "unknown scripted connector fault"


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
            # A job script reads its id as Slurm sets it (a service job derives
            # its port from it).
            env={**os.environ, "SLURM_JOB_ID": job_id},
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


@dataclass
class _ScheduledJob:
    job_id: str
    states: tuple[SlurmJobStatus, ...]
    pending_reason: str | None
    estimated_start: str | None
    position: int = 0

    @property
    def status(self) -> SlurmJobStatus:
        return self.states[self.position]

    def advance(self) -> None:
        self.position = min(self.position + 1, len(self.states) - 1)


@dataclass(frozen=True)
class _SubmitPlan:
    states: tuple[SlurmJobStatus, ...]
    pending_reason: str | None
    estimated_start: str | None
    lost_submit_reply: bool
    missing_exit_status: bool
    missing_stage_result: bool


class FakeConnector:
    """In-process connector with deterministic scheduler observations and reply loss.

    Staging and job scripts use the local-filesystem implementation of
    :func:`executing_cluster`. Scheduler transitions advance on inspection.
    Losing a reply retains the scheduler identity for reconciliation.
    """

    def __init__(self, state: Path) -> None:
        """Create an isolated executing cluster under the supplied directory."""
        self.state = executing_cluster(state)
        self._plans: dict[str, _SubmitPlan] = {}
        self._jobs: dict[str, _ScheduledJob] = {}
        self._accept_callbacks: dict[str, Callable[[], None]] = {}

    def on_accept(self, operation_id: str, callback: Callable[[], None]) -> None:
        """Run a deterministic synchronization barrier after scheduler acceptance."""
        self._accept_callbacks[f"vs-op-{operation_id}"] = callback

    def script(
        self,
        operation_id: str,
        *,
        states: tuple[SlurmJobStatus, ...] = (SlurmJobStatus.COMPLETED,),
        pending_reason: str | None = None,
        estimated_start: str | None = None,
        **faults: bool,
    ) -> None:
        """Configure observations plus lost_submit_reply or missing_exit_status faults."""
        if not states or any(not isinstance(state, SlurmJobStatus) for state in states):
            raise ValueError(_STATES_REQUIRED)
        unknown = faults.keys() - {
            "lost_submit_reply",
            "missing_exit_status",
            "missing_stage_result",
        }
        if unknown:
            message = f"{_UNKNOWN_FAULT}: {sorted(unknown)}"
            raise ValueError(message)
        self._plans[f"vs-op-{operation_id}"] = _SubmitPlan(
            states,
            pending_reason,
            estimated_start,
            faults.get("lost_submit_reply", False),
            faults.get("missing_exit_status", False),
            faults.get("missing_stage_result", False),
        )

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Answer a connector request without a transport subprocess."""
        del timeout
        if stdin is None:
            raise ValueError(_STDIN_REQUIRED)
        request = json.loads(stdin)
        tokens = shlex.split(str(request.get("command", "")))
        response = self._scheduler(tokens) if request["operation"] == "exec" else None
        if response is None:
            response = handle(self.state, request)
        else:
            with (self.state / REQUESTS_FILE).open("a", encoding="utf-8") as log:
                log.write(json.dumps(request) + "\n")
        if "sbatch" in tokens:
            self._accepted(tokens, response)
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    def _accepted(self, tokens: list[str], response: dict[str, object]) -> None:
        name = next(
            (token.split("=", 1)[1] for token in tokens if token.startswith("--job-name=")),
            "",
        )
        plan = self._plans.get(name)
        if plan is None:
            return
        job_id = str(response["stdout"]).split()[-1]
        self._jobs[name] = _ScheduledJob(
            job_id, plan.states, plan.pending_reason, plan.estimated_start
        )
        if plan.missing_exit_status or plan.missing_stage_result:
            start = tokens.index("sbatch")
            base = Path(tokens[tokens.index("cd") + 1]) if "cd" in tokens[:start] else Path.cwd()
            if plan.missing_exit_status:
                (base / "exit-code.txt").unlink(missing_ok=True)
            if plan.missing_stage_result:
                shutil.rmtree(base / "workspace" / ".vibesys-slurm-results" / "0001")
        callback = self._accept_callbacks.get(name)
        if callback is not None:
            callback()
        if plan.lost_submit_reply:
            raise OSError(_LOST_REPLY)

    def _scheduler(self, tokens: list[str]) -> dict[str, object] | None:
        if not tokens or tokens[0] not in {"squeue", "sacct", "scancel"}:
            return None
        output = ""
        if "-n" in tokens and tokens[0] == "squeue":
            job = self._jobs.get(tokens[tokens.index("-n") + 1])
            if job is not None and job.status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
                output = f"{job.job_id}\n"
        elif "--name" in tokens:
            job = self._jobs.get(tokens[tokens.index("--name") + 1])
            output = f"{job.job_id}\n" if job is not None else ""
        else:
            job_id = tokens[1] if tokens[0] == "scancel" else tokens[tokens.index("-j") + 1]
            job = next((item for item in self._jobs.values() if item.job_id == job_id), None)
            if job is None:
                return None
            output = self._job_output(job, tokens)
        return {"version": 1, "returncode": 0, "stdout": output, "stderr": ""}

    @staticmethod
    def _job_output(job: _ScheduledJob, tokens: list[str]) -> str:
        if tokens[0] == "scancel":
            if job.status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
                job.states = (SlurmJobStatus.CANCELLED,)
                job.position = 0
            return ""
        active = job.status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}
        if tokens[0] == "squeue":
            if not active:
                return ""
            output = job.status.value.upper()
            if "%T|%r|%S" in tokens:
                output += f"|{job.pending_reason or ''}|{job.estimated_start or ''}"
                job.advance()
            return output + "\n"
        if active:
            return ""
        output = f"{job.status.value.upper()} 0:0\n"
        job.advance()
        return output


def main(argv: Sequence[str] | None = None) -> int:
    """Answer the one request on stdin, or one SSH-transport program call."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    state = Path(arguments[0])
    program = arguments[1:2]
    if program == ["ssh"]:
        # ssh [options] -- HOST COMMAND
        response = handle(state, {"operation": "exec", "command": arguments[-1]})
        sys.stdout.write(str(response["stdout"]))
        sys.stderr.write(str(response["stderr"]))
        return int(str(response["returncode"]))
    if program == ["rsync"]:
        with (state / REQUESTS_FILE).open("a", encoding="utf-8") as log:
            log.write(json.dumps({"operation": "rsync", "argv": arguments[2:]}) + "\n")
        return 0
    sys.stdout.write(json.dumps(handle(state, json.loads(sys.stdin.read()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
