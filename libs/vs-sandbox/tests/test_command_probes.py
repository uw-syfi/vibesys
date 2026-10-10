"""Host status queries run through an injected ``CommandProbe``, so a Fake scripts what a tool printed."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_sandbox.api as sandbox_api
from vs_sandbox.api import NvidiaSmiTelemetry, SystemAcceleratorDiscovery
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe


@pytest.mark.parametrize(
    "answer",
    [None, ProbeResult(1, "0, GPU-a, A100, 1, 80, 3\n"), ProbeResult(127, "")],
    ids=["could-not-run", "nonzero-status", "not-found"],
)
def test_a_telemetry_query_that_does_not_succeed_reads_as_no_gpus(
    answer: ProbeResult | None,
) -> None:
    telemetry = NvidiaSmiTelemetry(probe=ScriptedProbe(lambda _argv: answer))

    assert telemetry.gpus() == []
    assert telemetry.compute_processes() == ""


def test_a_telemetry_query_names_the_configured_tool_and_its_timeout() -> None:
    probe = ScriptedProbe(lambda _argv: ProbeResult(0, "0, GPU-a, A100, 1, 80, 3\n"))

    gpus = NvidiaSmiTelemetry("/opt/nvidia-smi", 4.0, probe=probe).gpus()

    assert [gpu.index for gpu in gpus] == [0]
    ((argv, timeout),) = probe.calls
    assert (argv[0], timeout) == ("/opt/nvidia-smi", 4.0)


@given(rows=st.integers(min_value=0, max_value=12))
def test_the_rocm_device_count_is_the_csv_rows_after_the_header(rows: int) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "dev").mkdir()
        (root / "dev" / "kfd").touch()
        tool = root / "bin" / "rocm-smi"
        tool.parent.mkdir()
        tool.touch()
        tool.chmod(0o755)
        output = "device,id\n" + "".join(f"card{n},0x1\n" for n in range(rows))
        probe = ScriptedProbe(lambda _argv: ProbeResult(0, output))
        discovery = SystemAcceleratorDiscovery(
            device_root=root / "dev", executable_search_path=str(tool.parent), probe=probe
        )

        assert discovery.discover_rocm().reported_device_count == rows


@pytest.mark.parametrize("answer", [None, ProbeResult(2, "")], ids=["could-not-run", "failed"])
def test_a_failing_rocm_query_reports_no_count(tmp_path: Path, answer: ProbeResult | None) -> None:
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "kfd").touch()
    tool = tmp_path / "bin" / "rocm-smi"
    tool.parent.mkdir()
    tool.touch()
    tool.chmod(0o755)
    discovery = SystemAcceleratorDiscovery(
        device_root=tmp_path / "dev",
        executable_search_path=str(tool.parent),
        probe=ScriptedProbe(lambda _argv: answer),
    )

    assert discovery.discover_rocm().reported_device_count is None


@pytest.mark.parametrize("answer", [None, ProbeResult(1, "")], ids=["could-not-run", "blocked"])
def test_a_bwrap_that_cannot_create_a_namespace_is_not_a_confinement_backend(
    tmp_path: Path, answer: ProbeResult | None
) -> None:
    bwrap = tmp_path / "bin" / "bwrap"
    bwrap.parent.mkdir()
    bwrap.touch()
    bwrap.chmod(0o755)
    logs: list[str] = []
    probe = ScriptedProbe(lambda _argv: answer)

    sandbox = sandbox_api.build_host_sandbox(
        tmp_path,
        env={sandbox_api.SANDBOX_DISABLE_ENV: "bwrap", "PATH": str(bwrap.parent)},
        log=logs.append,
        probe=probe,
    )

    assert sandbox is None
    assert [argv[0] for argv, _timeout in probe.calls] == [str(bwrap)]
