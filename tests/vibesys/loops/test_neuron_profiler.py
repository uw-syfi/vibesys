"""The Neuron profiler drives neuron-explorer the way native TorchNeuron needs.

Guarantees under test:

* ``capture`` runs the workload with session device profiling: the runtime's default
  ("model") mode records nothing under TorchNeuron's asynchronous execution.
* Analysis points ``neuron-explorer view`` at the directory that holds the device
  trace, and names no NEFF itself, whatever else the capture root contains.
* ``capture`` with ``device_profile`` false records a system profile only (mode
  ``0``, no HBM reserved) and succeeds on the system trace alone.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest
from resources.profilers.neuron.server import build_server as neuron_server

if TYPE_CHECKING:
    from pathlib import Path

# A stand-in neuron-explorer: records its argv and device-profile mode, and for
# ``inspect -o DIR`` lays out a session the way the real runtime does: a system
# trace always, a device trace (NTFF) only when device profiling is on.
_FAKE_EXPLORER = """#!/bin/sh
log="$(dirname "$0")/calls.log"
printf '%s|mode=%s\\n' "$*" "${NEURON_RT_INSPECT_DEVICE_PROFILE:-unset}" >>"$log"
if [ "$1" = "inspect" ]; then
  session="$3/i-0_pid_7/1234"
  mkdir -p "$session" "$3/neffs"
  : >"$session/ntrace.pb"
  [ "${NEURON_RT_INSPECT_DEVICE_PROFILE:-0}" = "0" ] || : >"$session/profile_nc_0_session_0.ntff"
  : >"$session/neff_1_vnc_0.neff"
  : >"$session/neff_2_vnc_0.neff"
  : >"$3/neffs/barrier_0.neff"
fi
echo summary
"""


def _install_fake_explorer(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    executable = directory / "neuron-explorer"
    executable.write_text(_FAKE_EXPLORER)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))
    return directory / "calls.log"


def test_capture_records_a_session_device_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = _install_fake_explorer(bin_dir, monkeypatch)
    # A caller's own mode must not leak through: "model" yields no NTFF.
    monkeypatch.setenv("NEURON_RT_INSPECT_DEVICE_PROFILE", "model")

    asyncio.run(
        neuron_server().call_tool(
            "capture", {"workload": "true", "out_dir": str(tmp_path / "capture")}
        )
    )

    (inspect_call,) = calls.read_text().splitlines()
    assert inspect_call.startswith("inspect ")
    assert inspect_call.endswith("|mode=session")


@pytest.mark.parametrize("tool", ["summary", "summary_json", "view"])
def test_analysis_views_the_session_that_holds_the_device_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = _install_fake_explorer(bin_dir, monkeypatch)
    capture_root = tmp_path / "capture"
    asyncio.run(
        neuron_server().call_tool("capture", {"workload": "true", "out_dir": str(capture_root)})
    )

    asyncio.run(neuron_server().call_tool(tool, {"report": str(capture_root)}))

    view_call = calls.read_text().splitlines()[-1].split("|")[0].split()
    assert view_call[0] == "view"
    assert view_call[view_call.index("-d") + 1] == str(capture_root / "i-0_pid_7" / "1234")
    assert "-n" not in view_call
    assert "--neff-path" not in view_call


def test_a_system_only_capture_reserves_no_device_profile_and_needs_no_device_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workload too large for session profiling's HBM reservation is still capturable.

    Session device profiling reserves about 4.75 GiB per trn2 logical core; a 27B model
    using more than the remaining ~18.7 GiB failed to load under it in a live run.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = _install_fake_explorer(bin_dir, monkeypatch)

    asyncio.run(
        neuron_server().call_tool(
            "capture",
            {"workload": "true", "out_dir": str(tmp_path / "capture"), "device_profile": False},
        )
    )

    (inspect_call,) = calls.read_text().splitlines()
    assert inspect_call.endswith("|mode=0")
