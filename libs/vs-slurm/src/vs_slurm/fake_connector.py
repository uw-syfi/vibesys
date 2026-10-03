"""Executable Fake Slurm cluster speaking the versioned connector protocol.

Configure a connector transport whose command is
``python -m vs_slurm.fake_connector STATE_DIR`` and whose ``sbatch_command``
is the default ``sbatch``. Every submitted job stays
``PENDING`` until ``scancel`` cancels it, so a test can observe whether a
caller that stops early cancels its job. The connector keeps its state in
``STATE_DIR``:

- ``requests.jsonl``: every request, one JSON object per line, in order.
- ``submitted``: if this path exists (typically a FIFO the test reads), the
  connector writes the job id to it on submission. A FIFO lets a test block
  until the job exists without polling.
- ``cancelled``: created when ``scancel`` cancels the job.

Uploads and downloads touch nothing; content-cache probes report the object
as present, so staging never uploads.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

JOB_ID = "4242"
REQUESTS_FILE = "requests.jsonl"
SUBMITTED_FILE = "submitted"
CANCELLED_FILE = "cancelled"


def _exec(state: Path, command: str) -> str:
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
        submitted = state / SUBMITTED_FILE
        if submitted.exists():
            with submitted.open("w", encoding="utf-8") as handle:
                handle.write(JOB_ID)
        return f"Submitted batch job {JOB_ID}\n"
    return ""


def handle(state: Path, request: dict[str, object]) -> dict[str, object]:
    """Record one connector request and return its protocol response."""
    with (state / REQUESTS_FILE).open("a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    stdout = _exec(state, str(request["command"])) if request["operation"] == "exec" else ""
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
