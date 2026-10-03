"""The configured serving profile capture for a Slurm run.

One source for how a run's configured service is captured under its
representative load inside one Slurm job: the profiler MCP server's remote
bridge and the trusted evaluation executor both build their capture from here,
so an agent's ad hoc capture and the framework's trusted profile run the same
lifecycle.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

from vs_slurm.api import shell_join_with_port

if TYPE_CHECKING:
    from vs_sandbox.slurm_policy import SlurmExecutionPolicy
    from vs_slurm.api import SlurmConfig

# Remote path, relative to the job's workspace, that holds a trusted profile's
# result document and captures; the executor stages it back as one tree.
PROFILE_OUTPUT_ROOT = ".vibesys-profile"
_PORT_FILE = ".vibesys-profile-port"
_MIN_GRACE_SECONDS = 30.0
_MAX_GRACE_SECONDS = 120.0
_GRACE_FRACTION = 0.1
_MIN_JOB_MARGIN_SECONDS = 10.0
_MAX_JOB_MARGIN_SECONDS = 120.0
_JOB_MARGIN_FRACTION = 0.1


def configured_capture_lifecycle(
    config: SlurmConfig,
    policy: SlurmExecutionPolicy,
    benchmark_command: tuple[str, ...] | None,
) -> dict[str, object] | None:
    """Return the serving capture lifecycle, or ``None`` without a service and load.

    The service runs under the profiler on one dynamic port; the load is the
    trusted benchmark command; startup, grace, and timeout fit inside the job
    timeout.
    """
    service = policy.remote_service()
    if service is None or benchmark_command is None:
        return None
    job_timeout_s = float(config.job_timeout_seconds)
    completion_margin_s = min(
        _MAX_JOB_MARGIN_SECONDS,
        max(_MIN_JOB_MARGIN_SECONDS, job_timeout_s * _JOB_MARGIN_FRACTION),
    )
    timeout_s = max(1.0, job_timeout_s - completion_margin_s)
    grace_s = min(_MAX_GRACE_SECONDS, max(_MIN_GRACE_SECONDS, timeout_s * _GRACE_FRACTION))
    ready_timeout_s = min(float(service.startup_timeout_seconds), max(1.0, timeout_s - grace_s))
    read_port = f"read -r PORT < {shlex.quote(_PORT_FILE)}"
    setup_command = (
        f"{shlex.quote(policy.remote_python)} -c "
        + shlex.quote(
            "import socket; "
            "s=socket.socket(); "
            "s.bind(('127.0.0.1', 0)); "
            "print(s.getsockname()[1]); "
            "s.close()"
        )
        + f" > {shlex.quote(_PORT_FILE)}"
    )
    readiness_probe = shell_join_with_port(
        (
            policy.remote_python,
            "-c",
            "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2).close()",
            service.readiness_url,
        )
    )
    load = (*benchmark_command, *policy.benchmark_arguments)
    return {
        "command": f"{read_port}\n{shell_join_with_port(service.command)}",
        "cwd": None,
        "env": {},
        "ready_command": f"{read_port}\n{readiness_probe}",
        "ready_timeout_s": ready_timeout_s,
        "ready_interval_s": 1.0,
        "load_command": f"{read_port}\n{shell_join_with_port(load)}",
        "setup_command": setup_command,
        "stop_signal": "SIGINT",
        "grace_s": grace_s,
        "timeout_s": timeout_s,
    }


def trusted_profile_command(
    config: SlurmConfig,
    policy: SlurmExecutionPolicy,
    benchmark_command: tuple[str, ...] | None,
    *,
    profiler_tree: str,
) -> tuple[str, ...] | None:
    """Return the remote argv of one trusted timeline capture, or ``None`` when unconfigured.

    ``profiler_tree`` is the staged support tree holding the profiler's
    ``remote_capture.py``. The capture writes its result document and traces
    under :data:`PROFILE_OUTPUT_ROOT` and prints its summary to stdout.
    """
    lifecycle = configured_capture_lifecycle(config, policy, benchmark_command)
    if lifecycle is None:
        return None
    request = {"kind": "timeline", "lifecycle": lifecycle, "options": {}, "local_workspace": "."}
    return (
        policy.remote_python,
        f"{profiler_tree}/remote_capture.py",
        "--request-json",
        json.dumps(request, separators=(",", ":"), sort_keys=True),
        "--result",
        f"{PROFILE_OUTPUT_ROOT}/result.json",
        "--profiles",
        f"{PROFILE_OUTPUT_ROOT}/captures",
        "--print-output",
    )


__all__ = ["PROFILE_OUTPUT_ROOT", "configured_capture_lifecycle", "trusted_profile_command"]
