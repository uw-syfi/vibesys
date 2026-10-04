"""Trusted timeline descriptors select HIP APIs and verify measured rows on the host."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from resources.profilers.rocprof import remote_capture, semantic_capture

from vs_runtime.api import ProfileField
from vs_sandbox.api.slurm import (
    profile_capture_descriptor,
    require_profile_fields,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_KERNEL_FIXTURE = Path(__file__).parent / "fixtures/rocprof/kernel_trace/out_kernel_trace.csv"


def _command(workspace: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "rocprof_profiler/remote_capture.py",
        "--request-json",
        json.dumps(
            {
                "kind": "timeline",
                "lifecycle": {"command": "true", "cwd": str(workspace)},
                "options": {},
                "local_workspace": str(workspace),
            }
        ),
    )


def _request(command: Sequence[str]) -> str:
    return command[command.index("--request-json") + 1]


def _profiler(bin_dir: Path, *, emit_api: bool) -> None:
    bin_dir.mkdir()
    executable = bin_dir / "rocprofv3"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import shutil, sys\nfrom pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "out = Path(argv[argv.index('-d') + 1]); out.mkdir(parents=True, exist_ok=True)\n"
        f"shutil.copy({str(_KERNEL_FIXTURE)!r}, out / 'out_kernel_trace.csv')\n"
        f"if {emit_api!r} and '--hip-runtime-trace' in argv:\n"
        "    (out / 'out_hip_api_trace.csv').write_text("
        "'Function,Start_Timestamp,End_Timestamp\\nhipLaunchKernel,100,140\\n')\n"
    )
    executable.chmod(0o755)


@pytest.mark.parametrize("emit_api", [True, False], ids=["api-rows", "missing-api-rows"])
def test_real_semantic_producer_requires_hip_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    emit_api: bool,
) -> None:
    bin_dir = tmp_path / "bin"
    _profiler(bin_dir, emit_api=emit_api)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    command = require_profile_fields(_command(tmp_path), (ProfileField.HIP_API_TIMING,))
    request = json.loads(_request(command))
    assert request["options"]["hip_api"] is True
    assert request["required_fields"] == ["hip_api_timing"]

    code = semantic_capture.run(_request(command), tmp_path / "captures")
    output = capsys.readouterr()
    assert code == (0 if emit_api else 1)
    if emit_api:
        result = json.loads(output.out)
        assert result["artifact_refs"] == [(tmp_path / "captures").as_posix()]
        assert list((tmp_path / "captures").rglob("*_hip_api_trace.csv"))
    else:
        assert "hip_api_timing" in output.err
        assert not output.out


@given(fields=st.lists(st.sampled_from(ProfileField), unique=True).map(tuple))
def test_descriptor_never_silently_drops_requested_fields(fields: tuple[ProfileField, ...]) -> None:
    command = _command(Path("/candidate"))
    descriptor = profile_capture_descriptor(command)
    missing = set(fields) - set(descriptor.supported_fields)
    if missing:
        with pytest.raises(ValueError, match="cannot supply fields") as error:
            require_profile_fields(command, fields)
        assert all(field.value in str(error.value) for field in missing)
    else:
        selected = json.loads(_request(require_profile_fields(command, fields)))
        assert set(selected.get("required_fields", ())) == set(fields)
        assert selected["options"].get("hip_api", False) == bool(fields)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Function,Start_Timestamp,End_Timestamp\n",
        "Function,Elapsed\nhipLaunch,40\n",
        "Function,Start_Timestamp,End_Timestamp\nhipLaunch,bad,140\n",
        "Function,Start_Timestamp,End_Timestamp\nhipLaunch,nan,inf\n",
        "Function,Start_Timestamp,End_Timestamp\nhipLaunch,-10,140\n",
        "Function,Start_Timestamp,End_Timestamp\nhipLaunch,140,100\n",
    ],
)
def test_promised_hip_flag_without_timing_rows_is_missing(tmp_path: Path, body: str) -> None:
    capture = tmp_path / "timeline-0"
    capture.mkdir()
    (capture / "out_hip_api_trace.csv").write_text(body)
    assert remote_capture.missing_profile_fields(tmp_path, [capture.name], ["hip_api_timing"]) == [
        "hip_api_timing"
    ]


@given(
    start=st.integers(min_value=0, max_value=10**18),
    duration=st.integers(min_value=0, max_value=10**12),
)
def test_requested_hip_field_requires_a_valid_measured_interval(
    tmp_path_factory: pytest.TempPathFactory, start: int, duration: int
) -> None:
    profiles = tmp_path_factory.mktemp("api-interval")
    capture = profiles / "timeline-0"
    capture.mkdir()
    (capture / "out_hip_api_trace.csv").write_text(
        f"Function,Start_Timestamp,End_Timestamp\nhipLaunch,{start},{start + duration}\n"
    )
    assert remote_capture.missing_profile_fields(profiles, [capture.name], ["hip_api_timing"]) == []
