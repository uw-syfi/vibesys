"""Run a ROCprof capture request on a Slurm compute node.

This module is invoked only by the Slurm job runner. It delegates capture
and process supervision to the same functions exposed by the local MCP
server, then writes a small result document and leaves trace files under the
requested profile store for wrapper-mediated collection.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

for _common_name in ("_common", "profilers_common"):
    _candidate = _HERE.parent / _common_name
    if (_candidate / "capture_runtime.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break
import capture_runtime  # noqa: E402  # LW-930101; the preceding path setup must run before this import

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import capture  # noqa: E402  # LW-930102; the preceding path setup must run before this import

_CAPTURE_TOOLS = {
    "timeline": (
        capture.profile_timeline,
        {
            "hip_api",
            "kernel_include",
            "collection_delay_s",
            "collection_duration_s",
            "target",
        },
    ),
    "counters": (capture.profile_counters, {"sets", "kernel", "target"}),
    "kernel_deep": (capture.profile_kernel_deep, {"kernel", "dispatch", "target"}),
    "instructions": (
        capture.profile_instructions,
        {"kernel", "target_cu", "buffer_bytes", "target"},
    ),
    "ops": (
        capture.profile_ops,
        {
            "command",
            "cwd",
            "env",
            "ready_command",
            "ready_timeout_s",
            "load_command",
            "load_timeout_s",
            "setup_command",
            "stop_signal",
            "grace_s",
            "timeout_s",
            "delay_s",
            "duration_s",
            "record_shapes",
            "inject",
            "target",
        },
    ),
}
_LIFECYCLE_FIELDS = frozenset(
    {
        "command",
        "cwd",
        "env",
        "ready_command",
        "ready_timeout_s",
        "ready_interval_s",
        "load_command",
        "load_timeout_s",
        "setup_command",
        "stop_signal",
        "grace_s",
        "timeout_s",
    }
)


def run_request(
    request_path: Path | None,
    request_json: str | None,
    result_path: Path,
    profiles_path: Path,
    *,
    print_output: bool = False,
) -> int:
    """Execute one existing capture API call and persist its local result envelope.

    With ``print_output`` the capture's summary is also written to stdout, and
    a request that produced no capture fails: the trusted evaluation executor
    reads the job's output as the profile's evidence.
    """
    try:
        _prefer_active_python()
        request_text = (
            request_path.read_text(encoding="utf-8") if request_path is not None else request_json
        )
        if request_text is None:
            raise _RemoteCaptureRequestError.invalid_envelope()
        request = json.loads(request_text)
        request = _validated_request(request)
        kind = request["kind"]
        lifecycle_data = request["lifecycle"]
        options = request["options"]
        local_workspace = request["local_workspace"]
        function, _ = _CAPTURE_TOOLS[kind]
        rewritten_lifecycle = _rewrite_paths(lifecycle_data, local_workspace)
        lifecycle = None if kind == "ops" else capture_runtime.Lifecycle(**rewritten_lifecycle)
        profiles_path.mkdir(parents=True, exist_ok=True)
        old_ids = {path.name for path in profiles_path.iterdir() if path.is_dir()}
        os.environ["VIBESYS_PROFILE_DIR"] = str(profiles_path)
        if kind == "ops":
            output = function(**rewritten_lifecycle, **options)
        else:
            output = function(lifecycle, **options)
        new_ids = sorted(
            path.name
            for path in profiles_path.iterdir()
            if path.is_dir() and path.name not in old_ids
        )
        missing_fields = missing_profile_fields(
            profiles_path, new_ids, request.get("required_fields", [])
        )
        result = {
            "missing_fields": missing_fields,
            "output": output,
            "capture_ids": new_ids,
            "profiles_path": str(profiles_path),
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        capture_runtime.CaptureFailedError,
        capture_runtime.CaptureBusyError,
    ) as exc:
        sys.stderr.write(f"remote ROCprof capture failed: {exc}\n")
        return 1
    if missing_fields:
        sys.stderr.write(
            "not profilable: capture lacks required fields: " + ", ".join(missing_fields) + "\n"
        )
        return 1
    if not print_output:
        return 0
    sys.stdout.write(output if output.endswith("\n") else f"{output}\n")
    if not new_ids:
        sys.stderr.write("remote ROCprof capture produced no trace\n")
        return 1
    failure = workload_failure(profiles_path, new_ids)
    if failure is not None:
        # Written last so the end of the job output, which the trusted executor
        # keeps as the profile's summary, states why it is not profile evidence.
        sys.stdout.write(f"{failure}\n")
        return 1
    return 0


def missing_profile_fields(
    profiles: Path, capture_ids: list[str], required_fields: list[str]
) -> list[str]:
    """Require actual timestamped API rows, not just the requested profiler flag."""
    if "hip_api_timing" not in required_fields:
        return []
    for capture_id in capture_ids:
        for path in (profiles / capture_id).rglob("*_hip_api_trace.csv"):
            with path.open(newline="", encoding="utf-8") as stream:
                rows = csv.DictReader(stream)
                if any(_valid_api_timing(row) for row in rows):
                    return []
    return ["hip_api_timing"]


def _valid_api_timing(row: dict[str, str]) -> bool:
    try:
        start = int(row["Start_Timestamp"])
        end = int(row["End_Timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= start <= end


# One definition, shared with the agent-driven capture path.
workload_failure = capture_runtime.workload_failure


def _prefer_active_python() -> None:
    """Resolve ordinary Python commands through the configured remote runtime."""
    python_bin = str(Path(sys.executable).parent)
    current = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(part for part in (python_bin, current) if part)


def _validated_request(request: object) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) - {"required_fields"} != {
        "kind",
        "lifecycle",
        "options",
        "local_workspace",
    }:
        raise _RemoteCaptureRequestError.invalid_envelope()
    fields = request.get("required_fields", [])
    if not isinstance(fields, list) or any(field != "hip_api_timing" for field in fields):
        raise _RemoteCaptureRequestError.invalid_requirements()
    kind = request["kind"]
    if kind not in _CAPTURE_TOOLS:
        raise _RemoteCaptureRequestError.unknown_capture_kind()
    lifecycle_data = request["lifecycle"]
    options = request["options"]
    local_workspace = request["local_workspace"]
    if (
        not isinstance(lifecycle_data, dict)
        or not isinstance(options, dict)
        or not isinstance(local_workspace, str)
        or set(lifecycle_data) - _LIFECYCLE_FIELDS
    ):
        raise _RemoteCaptureRequestError.invalid_lifecycle()
    if fields and (kind != "timeline" or options.get("hip_api") is not True):
        raise _RemoteCaptureRequestError.invalid_requirements()
    _, allowed_options = _CAPTURE_TOOLS[kind]
    if set(options) - allowed_options:
        raise _RemoteCaptureRequestError.invalid_options()
    return request


class _RemoteCaptureRequestError(ValueError):
    """Actionable malformed remote capture request."""

    @classmethod
    def invalid_envelope(cls) -> _RemoteCaptureRequestError:
        return cls("request must contain kind, lifecycle, options, and local_workspace")

    @classmethod
    def unknown_capture_kind(cls) -> _RemoteCaptureRequestError:
        return cls("unknown ROCprof capture kind")

    @classmethod
    def invalid_lifecycle(cls) -> _RemoteCaptureRequestError:
        return cls("invalid ROCprof capture lifecycle")

    @classmethod
    def invalid_requirements(cls) -> _RemoteCaptureRequestError:
        return cls(
            "required_fields supports hip_api_timing only and requires timeline.options.hip_api=true"
        )

    @classmethod
    def invalid_options(cls) -> _RemoteCaptureRequestError:
        return cls("unrecognized ROCprof capture option")


def _rewrite_paths(value: dict[str, Any], local_workspace: str) -> dict[str, Any]:
    """Translate local candidate-root references to the staged job directory."""
    remote_workspace = Path.cwd().resolve()
    local_root = Path(local_workspace).resolve()
    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str):
            if key == "cwd":
                path = Path(item)
                try:
                    rewritten[key] = str(remote_workspace / path.resolve().relative_to(local_root))
                    continue
                except ValueError:
                    pass
            rewritten[key] = item.replace(str(local_root), str(remote_workspace))
        elif isinstance(item, dict):
            rewritten[key] = {
                str(nested_key): nested_value.replace(str(local_root), str(remote_workspace))
                if isinstance(nested_value, str)
                else nested_value
                for nested_key, nested_value in item.items()
            }
        else:
            rewritten[key] = item
    return rewritten


def main(argv: list[str] | None = None) -> int:
    """Parse one remote capture request and return its process status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--request-json")
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--print-output", action="store_true")
    parsed = parser.parse_args(argv)
    if (parsed.request is None) == (parsed.request_json is None):
        parser.error("provide exactly one of --request or --request-json")
    return run_request(
        parsed.request,
        parsed.request_json,
        parsed.result,
        parsed.profiles,
        print_output=parsed.print_output,
    )


if __name__ == "__main__":
    sys.exit(main())
