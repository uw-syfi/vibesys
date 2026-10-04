"""Executable Fake Slurm cluster speaking the versioned connector protocol.

Configure a connector transport whose command is
``python -m vs_slurm.fake_connector STATE_DIR`` and whose ``sbatch_command``
is the default ``sbatch``. The cluster has two modes, chosen by files in
``STATE_DIR``:

- Pending (default): the first submitted job is ``JOB_ID``; subsequent ids
  are unique. Every job stays ``PENDING``
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
  ``PENDING`` until ``scancel`` or :func:`release_job` starts its retained script.

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

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, Unpack

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
_MKDIR_BASE_POSITION = 2
_METADATA_FAILED = "Fake cluster metadata command failed"
_STATES_REQUIRED = "states must contain at least one scheduler observation"
_STDIN_REQUIRED = "connector stdin must contain one request"
_LOST_REPLY = "submit reply lost after scheduler acceptance"
_LOST_CLAIM_REPLY = "claim reply lost before intent publication"
_UNKNOWN_FAULT = "unknown scripted connector fault"


def executing_cluster(state: Path) -> Path:
    """Switch ``state`` to the executing mode and return it."""
    state.mkdir(parents=True, exist_ok=True)
    (state / RUN_FILE).touch()
    return state


def _pending_remote(state: Path, path: str) -> Path:
    return state / "remote" / path.lstrip("/")


def _pending_metadata(state: Path, command: str) -> str:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    paths = {word for word in lexer if word.startswith("/") and word != "/dev/null"}
    if not paths:
        translated = command
    else:
        replacements = {
            shlex.quote(path): shlex.quote(str(_pending_remote(state, path))) for path in paths
        }
        pattern = re.compile(
            "|".join(re.escape(path) for path in sorted(replacements, key=len, reverse=True))
        )
        translated = pattern.sub(lambda match: replacements[match.group()], command)
    code, output, _error = _executing_exec(state, translated)
    if code:
        raise OSError(_METADATA_FAILED)
    return output


def _pending_query(state: Path, tokens: list[str]) -> str:
    names = {
        path.read_text(encoding="utf-8"): path.stem
        for path in (state / _JOBS_DIRECTORY).glob("*.name")
    }
    if tokens[0] == "sacct":
        if "--name" in tokens:
            job_id = names.get(tokens[tokens.index("--name") + 1])
            return f"{job_id}\n" if job_id else ""
        job_state = _job_state(state, tokens[tokens.index("-j") + 1])
        return f"{job_state}\n" if job_state and job_state != _PENDING else "CANCELLED 0:0\n"
    if "-n" in tokens:
        job_id = names.get(tokens[tokens.index("-n") + 1])
        return f"{job_id}\n" if job_id and _job_state(state, job_id) == _PENDING else ""
    job_id = tokens[tokens.index("-j") + 1]
    job_state = _job_state(state, job_id)
    if (job_state and job_state != _PENDING) or (
        not job_state and (state / CANCELLED_FILE).exists()
    ):
        return ""
    return "PENDING||\n" if "%T|%r|%S" in tokens else "PENDING\n"


def _pending_submit(state: Path, tokens: list[str]) -> str:
    job_id, record = _allocate_job(state, first=int(JOB_ID))
    record.write_text(_PENDING, encoding="utf-8")
    name = next((token.split("=", 1)[1] for token in tokens if token.startswith("--job-name=")), "")
    if name:
        record.with_suffix(".name").write_text(name, encoding="utf-8")
    _announce(state, job_id)
    return f"Submitted batch job {job_id}\n"


def _pending_exec(state: Path, command: str) -> str:
    tokens = shlex.split(command)
    if ".cluster-operation" in command or ".cluster-cancelled" in command:
        return _pending_metadata(state, command)
    if tokens[:3] == ["if", "[", "-f"]:
        return "READY"
    if tokens[:1] == ["scancel"]:
        record = state / _JOBS_DIRECTORY / tokens[1]
        if record.exists():
            record.write_text("CANCELLED 0:0", encoding="utf-8")
        (state / CANCELLED_FILE).touch()
        return ""
    if tokens[:1] in (["squeue"], ["sacct"]):
        return _pending_query(state, tokens)
    if "sbatch" in tokens:
        return _pending_submit(state, tokens)
    return ""


def _announce(state: Path, job_id: str) -> None:
    submitted = state / SUBMITTED_FILE
    if submitted.exists():
        with submitted.open("w", encoding="utf-8") as handle:
            handle.write(job_id)


def _allocate_job(state: Path, *, first: int = _FIRST_EXECUTING_JOB) -> tuple[str, Path]:
    """Reserve a unique job id; connector processes run concurrently."""
    jobs = state / _JOBS_DIRECTORY
    jobs.mkdir(exist_ok=True)
    number = first
    while True:
        record = jobs / str(number)
        try:
            os.close(os.open(record, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            number += 1
            continue
        return str(number), record


def _submit(state: Path, tokens: list[str]) -> str:
    """Run one production job script, or retain its allocation while held."""
    job_id, record = _allocate_job(state)
    record.write_text(_PENDING, encoding="utf-8")
    record.with_suffix(".request.json").write_text(json.dumps(tokens), encoding="utf-8")
    if (state / HOLD_FILE).exists():
        _announce(state, job_id)
    else:
        release_job(state, job_id)
    return f"Submitted batch job {job_id}\n"


def active_jobs(state: Path) -> tuple[str, ...]:
    """Return numeric scheduler identities whose allocations remain nonterminal."""
    return tuple(
        sorted(
            path.name
            for path in (state / _JOBS_DIRECTORY).glob("*")
            if path.name.isdecimal() and path.read_text(encoding="utf-8") in {_PENDING, "RUNNING"}
        )
    )


def pending_jobs(state: Path) -> tuple[str, ...]:
    """Return allocations held by the scheduler before their script starts."""
    return tuple(job_id for job_id in active_jobs(state) if _job_state(state, job_id) == _PENDING)


def release_jobs(state: Path) -> None:
    """Start all held allocations and allow subsequent submissions to execute."""
    (state / HOLD_FILE).unlink(missing_ok=True)
    for job_id in pending_jobs(state):
        release_job(state, job_id)


def release_job(state: Path, job_id: str) -> None:
    """Start a pending executing allocation; terminal jobs cannot run again.

    The retained directory, script, output path and numeric scheduler identity
    are the ones accepted at submission. A cancelled allocation stays cancelled.
    """
    record = state / _JOBS_DIRECTORY / job_id
    if not (state / RUN_FILE).exists():
        message = "only executing Fake Slurm allocations can be released"
        raise ValueError(message)
    if _job_state(state, job_id) != _PENDING:
        return
    tokens = json.loads(record.with_suffix(".request.json").read_text(encoding="utf-8"))
    start = tokens.index("sbatch")
    directory = Path(tokens[tokens.index("cd") + 1]) if "cd" in tokens[:start] else Path.cwd()
    output = next(
        Path(token.split("=", 1)[1]) for token in tokens[start:] if token.startswith("--output=")
    )
    script = Path(tokens[-1])
    record.write_text("RUNNING", encoding="utf-8")
    with output.open("w", encoding="utf-8") as log:
        # lint-waiver: LW-140001 [S603]; the Fake cluster runs the exact
        # > production-generated job script; reading the script instead of
        # > running it would not produce the result files callers download.
        completed = subprocess.run(  # noqa: S603
            ("/bin/bash", str(script)),
            cwd=directory,
            env={**os.environ, "SLURM_JOB_ID": job_id},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    state_line = (
        "COMPLETED 0:0" if completed.returncode == 0 else f"FAILED {completed.returncode}:0"
    )
    record.write_text(state_line, encoding="utf-8")


def _job_state(state: Path, job_id: str) -> str:
    record = state / _JOBS_DIRECTORY / job_id
    return record.read_text(encoding="utf-8") if record.is_file() else ""


def _executing_exec(state: Path, command: str) -> tuple[int, str, str]:
    tokens = shlex.split(command)
    if "sbatch" in tokens:
        return 0, _submit(state, tokens), ""
    if tokens[:1] == ["squeue"]:
        job_state = _job_state(state, tokens[3])
        return 0, (f"{job_state}\n" if job_state in {_PENDING, "RUNNING"} else ""), ""
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
    if request["operation"] == "put" and (
        ".cluster-operation" in str(request["remote_path"])
        or ".cluster-cancelled" in str(request["remote_path"])
    ):
        _copy(Path(str(request["local_path"])), _pending_remote(state, str(request["remote_path"])))
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
    rejected_reason: str | None
    lost_claim_reply: bool


class _ConnectorOptions(TypedDict, total=False):
    pending_reason: str | None
    estimated_start: str | None
    lost_submit_reply: bool
    missing_exit_status: bool
    missing_stage_result: bool
    rejected_reason: str
    lost_claim_reply: bool
    on_dispatch: Callable[[], None]


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
        self._forgotten_names: set[str] = set()
        self._accept_callbacks: dict[str, Callable[[], None]] = {}
        self._dispatch_callbacks: dict[str, Callable[[], None]] = {}
        self._dispatched: set[str] = set()

    def forget_name_history(self, operation_id: str | None = None) -> None:
        """Expire name-based scheduler history while retaining numeric job observations."""
        suffix = hashlib.sha256(operation_id.encode()).hexdigest()[:32] if operation_id else None
        self._forgotten_names.update(
            name for name in self._jobs if suffix is None or name.endswith(suffix)
        )

    def on_accept(self, operation_id: str, callback: Callable[[], None]) -> None:
        """Run a deterministic synchronization barrier after scheduler acceptance."""
        self._accept_callbacks[hashlib.sha256(operation_id.encode()).hexdigest()[:32]] = callback

    def script(
        self,
        operation_id: str,
        *,
        states: tuple[SlurmJobStatus, ...] = (SlurmJobStatus.COMPLETED,),
        **options: Unpack[_ConnectorOptions],
    ) -> None:
        """Configure scheduler observations and explicitly named transport faults."""
        if not states or any(not isinstance(state, SlurmJobStatus) for state in states):
            raise ValueError(_STATES_REQUIRED)
        unknown = options.keys() - _ConnectorOptions.__annotations__.keys()
        if unknown:
            message = f"{_UNKNOWN_FAULT}: {sorted(unknown)}"
            raise ValueError(message)
        name = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        self._plans[name] = _SubmitPlan(
            states,
            options.get("pending_reason"),
            options.get("estimated_start"),
            options.get("lost_submit_reply", False),
            options.get("missing_exit_status", False),
            options.get("missing_stage_result", False),
            options.get("rejected_reason"),
            options.get("lost_claim_reply", False),
        )
        callback = options.get("on_dispatch")
        if callback is not None:
            self._dispatch_callbacks[name] = callback

    def _before_dispatch(self, tokens: list[str]) -> None:
        if (
            tokens[:2] != ["mkdir", "-p"]
            or len(tokens) <= _MKDIR_BASE_POSITION + 1
            or "if" in tokens
        ):
            return
        name = hashlib.sha256(Path(tokens[2]).name.encode()).hexdigest()[:32]
        plan = self._plans.get(name)
        if plan is None or name in self._dispatched:
            return
        self._dispatched.add(name)
        callback = self._dispatch_callbacks.get(name)
        if callback is not None:
            callback()
        if plan.rejected_reason is not None:
            raise OSError(plan.rejected_reason)

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Answer a connector request without a transport subprocess."""
        del timeout
        if stdin is None:
            raise ValueError(_STDIN_REQUIRED)
        request = json.loads(stdin)
        tokens = shlex.split(str(request.get("command", "")))
        self._before_dispatch(tokens)
        response = self._scheduler(tokens) if request["operation"] == "exec" else None
        if response is None:
            response = handle(self.state, request)
        else:
            with (self.state / REQUESTS_FILE).open("a", encoding="utf-8") as log:
                log.write(json.dumps(request) + "\n")
        if response["stdout"] == "CREATED" and tokens[:2] == ["mkdir", "-p"]:
            plan = self._plans.get(hashlib.sha256(Path(tokens[2]).name.encode()).hexdigest()[:32])
            if plan is not None and plan.lost_claim_reply:
                raise OSError(_LOST_CLAIM_REPLY)
        if "sbatch" in tokens:
            self._accepted(tokens, response)
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    def _accepted(self, tokens: list[str], response: dict[str, object]) -> None:
        name = next(
            (token.split("=", 1)[1] for token in tokens if token.startswith("--job-name=")),
            "",
        )
        operation_hash = name.rsplit("-", 1)[-1]
        plan = self._plans.get(operation_hash)
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
        callback = self._accept_callbacks.get(operation_hash)
        if callback is not None:
            callback()
        if plan.lost_submit_reply:
            raise OSError(_LOST_REPLY)

    def _scheduler(self, tokens: list[str]) -> dict[str, object] | None:
        if not tokens or tokens[0] not in {"squeue", "sacct", "scancel"}:
            return None
        output = ""
        if "-n" in tokens and tokens[0] == "squeue":
            name = tokens[tokens.index("-n") + 1]
            job = None if name in self._forgotten_names else self._jobs.get(name)
            if job is not None and job.status in {SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING}:
                output = f"{job.job_id}\n"
        elif "--name" in tokens:
            name = tokens[tokens.index("--name") + 1]
            job = None if name in self._forgotten_names else self._jobs.get(name)
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


def _rsync(state: Path, arguments: list[str]) -> None:
    operands = arguments[arguments.index("--") + 1 :]
    source, destination = operands
    remote = destination if ":" in destination else source
    executing = (state / RUN_FILE).exists()
    metadata = ".cluster-operation" in remote or ".cluster-cancelled" in remote
    if not executing and not metadata:
        return

    def path(value: str) -> Path:
        if ":" not in value:
            return Path(value)
        remote_path = value.split(":", 1)[1]
        return Path(remote_path) if executing else _pending_remote(state, remote_path)

    source_path, destination_path = path(source), path(destination)
    if source_path.is_dir():
        if "--delete" in arguments and destination_path.exists():
            shutil.rmtree(destination_path)
        excludes = [
            item.split("=", 1)[1].strip("/") for item in arguments if item.startswith("--exclude=")
        ]
        shutil.copytree(
            source_path,
            destination_path,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=shutil.ignore_patterns(*excludes),
        )
    else:
        _copy(source_path, destination_path)


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
        _rsync(state, arguments[2:])
        return 0
    sys.stdout.write(json.dumps(handle(state, json.loads(sys.stdin.read()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
