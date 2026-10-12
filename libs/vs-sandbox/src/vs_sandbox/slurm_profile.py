"""The configured serving profile capture for a Slurm run.

One source for how a run's configured service is captured under its
trusted diagnostic load inside one Slurm job: the profiler MCP server's remote
bridge and the trusted evaluation executor both build their capture from here,
so an agent's ad hoc capture and the framework's trusted profile run the same
lifecycle.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vs_evaluation.api import EvidenceMetric, ProfileField
from vs_slurm.api import PORT_PLACEHOLDER, shell_join_with_port

if TYPE_CHECKING:
    from vs_sandbox.slurm_policy import SlurmExecutionPolicy
    from vs_slurm.api import SlurmConfig

# Remote path, relative to the job's workspace, that holds a trusted profile's
# result document and captures; the executor stages it back as one tree.
PROFILE_OUTPUT_ROOT = ".vibesys-profile"
# The capture's last stdout line when it has typed statistics: this prefix, then one
# `ProfileStats` JSON object. The remote script cannot import this library, so it spells
# the prefix itself and a test keeps the two equal.
PROFILE_STATS_PREFIX = "VIBESYS_PROFILE_STATS "
GPU_ACTIVE_FRACTION_METRIC = "gpu_active_fraction"
HIP_API_TIME_METRIC_PREFIX = "hip_api_time_ms."
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
    profile_command: tuple[str, ...] | None,
    *,
    workload_timeout_seconds: int | None = None,
) -> dict[str, object] | None:
    """Return the capture lifecycle the run can execute, or ``None`` when it has none.

    With a configured service, the service runs under the profiler on one dynamic
    port and the load is the trusted profiling command; ``profile.command`` is then
    required, and a benchmark is never a fallback. Without a service the profiling
    command is itself the profiled target and runs to completion under the profiler
    (a kernel or script workload), so no signal ever stops it; with no command there
    is nothing to capture and the result is ``None``. Startup, grace, and timeout
    fit inside the job timeout.
    """
    service = policy.remote_service()
    if service is None:
        return _command_capture_lifecycle(config, profile_command, workload_timeout_seconds)
    if profile_command is None:
        message = "profile.command is required for a configured serving capture"
        raise ValueError(message)
    timeout_s, grace_s = _job_budget(config)
    ready_timeout_s = min(float(service.startup_timeout_seconds), max(1.0, timeout_s - grace_s))
    if workload_timeout_seconds is not None:
        timeout_s = min(timeout_s, ready_timeout_s + workload_timeout_seconds + grace_s)
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
    return {
        "command": f"{read_port}\n{shell_join_with_port(service.command)}",
        "cwd": None,
        "env": {},
        "ready_command": f"{read_port}\n{readiness_probe}",
        "ready_timeout_s": ready_timeout_s,
        "ready_interval_s": 1.0,
        "load_timeout_s": workload_timeout_seconds,
        "load_command": f"{read_port}\n{shell_join_with_port(profile_command)}",
        "setup_command": setup_command,
        "stop_signal": "SIGINT",
        "grace_s": grace_s,
        "timeout_s": timeout_s,
    }


def _job_budget(config: SlurmConfig) -> tuple[float, float]:
    """The capture's overall timeout and stop grace, both fitted inside the job timeout."""
    job_timeout_s = float(config.job_timeout_seconds)
    completion_margin_s = min(
        _MAX_JOB_MARGIN_SECONDS,
        max(_MIN_JOB_MARGIN_SECONDS, job_timeout_s * _JOB_MARGIN_FRACTION),
    )
    timeout_s = max(1.0, job_timeout_s - completion_margin_s)
    grace_s = min(_MAX_GRACE_SECONDS, max(_MIN_GRACE_SECONDS, timeout_s * _GRACE_FRACTION))
    return timeout_s, grace_s


def _command_capture_lifecycle(
    config: SlurmConfig,
    profile_command: tuple[str, ...] | None,
    workload_timeout_seconds: int | None,
) -> dict[str, object] | None:
    """The lifecycle of a command-only target: it exits by itself, so no stop signal is used."""
    if profile_command is None:
        return None
    if any(PORT_PLACEHOLDER in argument for argument in profile_command):
        message = "profile.command uses the dynamic port, which needs a [vibesys.service]"
        raise ValueError(message)
    timeout_s, grace_s = _job_budget(config)
    if workload_timeout_seconds is not None:
        timeout_s = min(timeout_s, workload_timeout_seconds + grace_s)
    return {
        "command": shlex.join(profile_command),
        "cwd": None,
        "env": {},
        "stop_signal": "SIGINT",
        "grace_s": grace_s,
        "timeout_s": timeout_s,
    }


def trusted_profile_command(
    config: SlurmConfig,
    policy: SlurmExecutionPolicy,
    profile_command: tuple[str, ...] | None,
    *,
    profiler_tree: str,
    workload_timeout_seconds: int | None = None,
) -> tuple[str, ...] | None:
    """Return the remote argv of one trusted timeline capture, or ``None`` when unconfigured.

    ``profiler_tree`` is the staged support tree holding the profiler's
    ``remote_capture.py``. The capture writes its result document and traces
    under :data:`PROFILE_OUTPUT_ROOT` and prints its summary to stdout.
    """
    lifecycle = configured_capture_lifecycle(
        config, policy, profile_command, workload_timeout_seconds=workload_timeout_seconds
    )
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


class ProfileCaptureDescriptor(BaseModel):
    """Declared measurement support of a trusted remote capture command."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    supported_fields: tuple[ProfileField, ...] = ()


def profile_capture_descriptor(command: tuple[str, ...] | None) -> ProfileCaptureDescriptor:
    """Derive support from the capture request actually selected by wiring."""
    if command is None or "--request-json" not in command:
        return ProfileCaptureDescriptor()
    request = json.loads(command[command.index("--request-json") + 1])
    if request.get("kind") == "timeline":
        return ProfileCaptureDescriptor(supported_fields=(ProfileField.HIP_API_TIMING,))
    return ProfileCaptureDescriptor()


def require_profile_fields(
    command: tuple[str, ...], fields: tuple[ProfileField, ...]
) -> tuple[str, ...]:
    """Select supported fields in the trusted descriptor without changing its lifecycle."""
    if not fields:
        return command
    descriptor = profile_capture_descriptor(command)
    missing = set(fields) - set(descriptor.supported_fields)
    if missing:
        message = "configured capture cannot supply fields: " + ", ".join(sorted(missing))
        raise ValueError(message)
    index = command.index("--request-json") + 1
    request = json.loads(command[index])
    request["options"]["hip_api"] = True
    request["required_fields"] = sorted(set(fields))
    return (
        *command[:index],
        json.dumps(request, separators=(",", ":"), sort_keys=True),
        *command[index + 1 :],
    )


class HipApiTime(BaseModel):
    """Total time one HIP API call name took in the captured window."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    total_ms: float = Field(ge=0, allow_inf_nan=False)
    calls: int = Field(ge=0)


class ProfileStats(BaseModel):
    """Headline statistics of a timeline capture, as the capture itself computed them."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    gpu_active_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    top_hip_api: tuple[HipApiTime, ...] = ()

    def metrics(self) -> tuple[EvidenceMetric, ...]:
        """The statistics as evidence metrics: the one typed channel readings decode."""
        active = (
            ()
            if self.gpu_active_fraction is None
            else (
                EvidenceMetric(
                    name=GPU_ACTIVE_FRACTION_METRIC,
                    value=self.gpu_active_fraction,
                    direction="max",
                    unit="fraction",
                ),
            )
        )
        return (
            *active,
            *(
                EvidenceMetric(
                    name=f"{HIP_API_TIME_METRIC_PREFIX}{item.name}",
                    value=item.total_ms,
                    direction="min",
                    unit="ms",
                )
                for item in self.top_hip_api
            ),
        )


def split_profile_stats(stdout: str) -> tuple[str, ProfileStats | None]:
    """Separate a capture's printed summary from its typed statistics line.

    Returns the text without the statistics line, and the parsed statistics, or None
    when the capture printed none. A statistics line that does not parse raises
    ``ValueError`` naming the problem: the capture's own record is never guessed at.
    """
    kept: list[str] = []
    stats: ProfileStats | None = None
    for line in stdout.splitlines(keepends=True):
        if not line.startswith(PROFILE_STATS_PREFIX):
            kept.append(line)
            continue
        try:
            stats = ProfileStats.model_validate_json(line[len(PROFILE_STATS_PREFIX) :])
        except ValidationError as exc:
            message = f"malformed profile statistics line: {exc.error_count()} invalid field(s)"
            raise ValueError(message) from exc
    return "".join(kept), stats


__all__ = [
    "GPU_ACTIVE_FRACTION_METRIC",
    "HIP_API_TIME_METRIC_PREFIX",
    "PROFILE_OUTPUT_ROOT",
    "PROFILE_STATS_PREFIX",
    "HipApiTime",
    "ProfileCaptureDescriptor",
    "ProfileStats",
    "configured_capture_lifecycle",
    "profile_capture_descriptor",
    "require_profile_fields",
    "split_profile_stats",
    "trusted_profile_command",
]
