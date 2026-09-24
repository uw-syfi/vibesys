"""Tests for accelerator device passthrough into the bubblewrap namespace."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vs_sandbox import api as sandbox_api
from vs_sandbox import host_sandbox
from vs_sandbox.api import HostSandbox


def _host_devices(pattern: str) -> list[Path]:
    dev = Path("/dev")
    return sorted(dev.glob(pattern)) if dev.exists() else []


def _build_host_sandbox(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> HostSandbox:
    monkeypatch.setattr(host_sandbox.shutil, "which", lambda *_args, **_kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr(host_sandbox, "_bwrap_confines", lambda _bwrap: True)
    sandbox = sandbox_api.build_host_sandbox(workspace, env={})
    assert isinstance(sandbox, HostSandbox)
    return sandbox


class TestAcceleratorDeviceNodes:
    """``--dev`` mounts a minimal devtmpfs, so accelerators need rebinding.

    Without this the agent can edit code for an accelerator it cannot run,
    which fails as a confusing driver error rather than a policy error.
    """

    @pytest.mark.parametrize("pattern", ["neuron*", "nvidia*"])
    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="host backend is Linux-only")
    def test_host_accelerator_devices_are_passed_through(
        self,
        pattern: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        devices = _host_devices(pattern)
        if not devices:
            pytest.skip(f"host has no /dev/{pattern} devices")

        sandbox = _build_host_sandbox(monkeypatch, tmp_path)
        command = sandbox.wrap(["true"])
        nodes = {
            Path(command[index + 1])
            for index, argument in enumerate(command[:-1])
            if argument == "--dev-bind-try"
        }

        assert set(devices).issubset(nodes)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="host backend is Linux-only")
    def test_bound_accelerator_paths_exist(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        sandbox = _build_host_sandbox(monkeypatch, tmp_path)
        command = sandbox.wrap(["true"])
        nodes = [
            Path(command[index + 1])
            for index, argument in enumerate(command[:-1])
            if argument == "--dev-bind-try"
        ]
        assert all(node.exists() for node in nodes)
