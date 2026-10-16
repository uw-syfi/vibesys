"""``backend_provided``: the one answer to which backends a host can run."""

from __future__ import annotations

import sys

import pytest

from vs_sandbox.api import ComputeBackend, backend_provided
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe

_GPU_ROW = "0, GPU-1, NVIDIA A100, 10, 40960, 3\n"


def _host(nvidia_smi: ProbeResult | None) -> ScriptedProbe:
    """A host whose ``nvidia-smi`` answers ``nvidia_smi`` and that runs nothing else."""
    return ScriptedProbe(lambda argv: nvidia_smi if argv[0] == "nvidia-smi" else None)


@pytest.mark.parametrize("answer", [None, ProbeResult(0, ""), ProbeResult(9, "", "failed")])
def test_cpu_is_provided_by_every_host_and_cuda_by_none_without_a_gpu(
    monkeypatch: pytest.MonkeyPatch, answer: ProbeResult | None
) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    assert backend_provided(ComputeBackend.CPU, _host(answer))
    assert not backend_provided(ComputeBackend.CUDA, _host(answer))


def test_cuda_is_provided_by_a_host_that_lists_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    assert backend_provided(ComputeBackend.CUDA, _host(ProbeResult(0, _GPU_ROW)))


@pytest.mark.parametrize(("platform", "expected"), [("darwin", True), ("linux", False)])
def test_metal_is_provided_by_macos_only(
    monkeypatch: pytest.MonkeyPatch, platform: str, *, expected: bool
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    assert backend_provided(ComputeBackend.METAL, _host(None)) is expected
