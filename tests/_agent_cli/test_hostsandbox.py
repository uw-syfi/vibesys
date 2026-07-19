"""Tests for host-path workspace confinement (issue #149).

Two layers:

* Pure-Python unit tests for the policy builder and ``bwrap`` argv construction
  (no subprocess, run everywhere).
* An end-to-end regression that drives the real agent launch chokepoint
  (:meth:`CLICodingAgent.generate`) with a stub agent that tries to escape its
  workspace. These are skipped unless a working ``bwrap`` + user-namespace stack
  is present, since that is what actually enforces the boundary.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from vibesys._agent_cli import hostsandbox
from vibesys._agent_cli.cli_agent import CLICodingAgent

# ---------------------------------------------------------------------------
# Real-sandbox availability probe
# ---------------------------------------------------------------------------


def _bwrap_works() -> bool:
    """True if bwrap can actually create a user namespace on this host."""
    if not sys.platform.startswith("linux"):
        return False
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    probe = "/usr/bin/true" if Path("/usr/bin/true").exists() else "/bin/true"
    argv = [bwrap, "--unshare-user"]
    for root in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(root).exists():
            argv += ["--ro-bind", root, root]
    argv.append(probe)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


requires_sandbox = pytest.mark.skipif(
    not _bwrap_works(),
    reason="requires a working bwrap + user-namespace stack",
)


# ---------------------------------------------------------------------------
# Policy builder
# ---------------------------------------------------------------------------


class TestBuild:
    def test_disabled_via_env_returns_none(self, tmp_path):
        logs: list[str] = []
        sb = hostsandbox.build(tmp_path, env={hostsandbox.DISABLE_ENV: "0"}, log=logs.append)
        assert sb is None
        assert any("DISABLED" in m for m in logs)

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE", " Off "])
    def test_disable_values(self, tmp_path, value):
        assert hostsandbox.build(tmp_path, env={hostsandbox.DISABLE_ENV: value}) is None

    def test_missing_bwrap_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostsandbox.sys, "platform", "linux")
        monkeypatch.setattr(hostsandbox.shutil, "which", lambda *a, **k: None)
        logs: list[str] = []
        assert hostsandbox.build(tmp_path, env={}, log=logs.append) is None
        assert any("bwrap" in m for m in logs)

    def test_non_linux_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostsandbox.sys, "platform", "darwin")
        logs: list[str] = []
        assert hostsandbox.build(tmp_path, env={}, log=logs.append) is None
        assert any("unavailable" in m for m in logs)

    def test_allow_ancestor_of_workspace_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostsandbox.sys, "platform", "linux")
        monkeypatch.setattr(hostsandbox.shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
        workspace = tmp_path / "exp_env" / "run" / "workspace"
        workspace.mkdir(parents=True)
        logs: list[str] = []
        sb = hostsandbox.build(
            workspace,
            env={hostsandbox.ALLOW_ENV: str(tmp_path)},  # ancestor => sibling leak
            log=logs.append,
        )
        assert sb is not None
        assert tmp_path.resolve() not in sb.read_paths
        assert any("ancestor" in m for m in logs)

    def test_allow_extra_path_is_bound(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostsandbox.sys, "platform", "linux")
        monkeypatch.setattr(hostsandbox.shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        weights = tmp_path.parent / f"{tmp_path.name}-weights"
        weights.mkdir()
        try:
            sb = hostsandbox.build(workspace, env={hostsandbox.ALLOW_ENV: str(weights)})
            assert sb is not None
            assert weights.resolve() in sb.read_paths
        finally:
            weights.rmdir()


# ---------------------------------------------------------------------------
# bwrap argv construction (no subprocess)
# ---------------------------------------------------------------------------


class TestWrap:
    def _sandbox(self, workspace: Path) -> hostsandbox.HostSandbox:
        return hostsandbox.HostSandbox(
            bwrap_path="/usr/bin/bwrap",
            workspace=workspace,
            read_paths=(Path("/opt/toolchain"),),
            write_paths=(Path("/home/u/.codex"),),
            system_read_roots=("/usr",),
            gpu_device_nodes=(Path("/dev/nvidia0"),),
        )

    def test_wrap_shape(self, tmp_path):
        sb = self._sandbox(tmp_path)
        argv = sb.wrap(["codex", "exec", "--json"])
        ws = str(tmp_path)
        assert argv[0] == "/usr/bin/bwrap"
        # Workspace is bound read-write and becomes cwd.
        assert _has_pair(argv, "--bind", ws, ws)
        assert _has_pair(argv, "--chdir", ws)
        # Toolchain read-only, config read-write, GPU node passed through.
        assert _has_pair(argv, "--ro-bind-try", "/opt/toolchain", "/opt/toolchain")
        assert _has_pair(argv, "--bind-try", "/home/u/.codex", "/home/u/.codex")
        assert _has_pair(argv, "--dev-bind-try", "/dev/nvidia0", "/dev/nvidia0")
        # Network is deliberately *not* unshared.
        assert "--unshare-net" not in argv
        assert "--unshare-all" not in argv
        # The wrapped command is preserved verbatim after the ``--`` separator.
        sep = argv.index("--")
        assert argv[sep + 1 :] == ["codex", "exec", "--json"]

    def test_workspace_bind_wins_over_readonly(self, tmp_path):
        """The rw workspace bind must come after ro binds so it takes effect."""
        sb = self._sandbox(tmp_path)
        argv = sb.wrap(["agent"])
        last_ro = max(i for i, a in enumerate(argv) if a == "--ro-bind-try")
        ws_bind = argv.index("--bind")
        assert ws_bind > last_ro


def _has_pair(argv: list[str], flag: str, *operands: str) -> bool:
    for i, tok in enumerate(argv):
        if tok == flag and argv[i + 1 : i + 1 + len(operands)] == list(operands):
            return True
    return False


# ---------------------------------------------------------------------------
# End-to-end regression through the real launch path (issue #149)
# ---------------------------------------------------------------------------


class _StubAgent(CLICodingAgent):
    """A concrete CLICodingAgent that runs a fixed script, skipping binary
    detection — mirrors the pattern in ``test_codex.py``."""

    def __init__(self, script: str):
        from agentshim.executor import HostCommandExecutor

        self.executor = HostCommandExecutor()
        self.env = dict(os.environ)
        self.binary_name = "stub-agent"
        self.binary_path = script
        self.model = None
        self.event_handler = None
        import loguru

        self.logger = loguru.logger
        self.session_id = None
        self.sandbox = None
        self._script = script

    def _get_command(self, prompt: str) -> list[str]:
        return [self._script]


def _escape_probe(tmp_path_factory) -> tuple[_StubAgent, Path, Path]:
    """Set up a workspace with a sibling secret and a stub agent (in a separate
    toolchain dir, like a real agent binary) that tries to read/write it."""
    host = tmp_path_factory.mktemp("host")
    tool = tmp_path_factory.mktemp("tool")
    workspace = host / "exp_env" / "run-A" / "workspace"
    workspace.mkdir(parents=True)
    sibling = host / "exp_env" / "run-B-secret"
    sibling.mkdir(parents=True)
    (sibling / "creds.txt").write_text("SECRET=leak\n")

    script = tool / "stub_agent.sh"
    script.write_text(
        "#!/bin/sh\n"
        "cat - > /dev/null 2>&1\n"
        f'cat "{sibling / "creds.txt"}" 2>/dev/null && echo READ_OK\n'
        f'echo pwn > "{sibling / "pwn.txt"}" 2>/dev/null && echo WRITE_OK\n'
        "echo ok > ./candidate.txt 2>/dev/null && echo WS_OK\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return _StubAgent(str(script)), workspace, sibling


def test_reproduces_escape_without_sandbox(tmp_path_factory):
    """Without confinement the stub agent reaches the sibling run — the #149 bug."""
    agent, workspace, _ = _escape_probe(tmp_path_factory)
    agent.sandbox = None  # legacy behavior
    out = agent.generate("stay in the workspace", cwd=str(workspace), silent=True)
    assert "READ_OK" in out and "SECRET=leak" in out
    assert "WRITE_OK" in out


@requires_sandbox
def test_sandbox_blocks_escape_but_allows_workspace(tmp_path_factory):
    """With confinement installed the same escape attempts fail, yet the agent
    can still read and write inside its own workspace."""
    agent, workspace, sibling = _escape_probe(tmp_path_factory)
    agent.sandbox = hostsandbox.build(workspace, env=agent.env, binary_path=agent.binary_path)
    assert agent.sandbox is not None
    out = agent.generate("stay in the workspace", cwd=str(workspace), silent=True)

    assert "READ_OK" not in out and "SECRET=leak" not in out
    assert "WRITE_OK" not in out
    assert not (sibling / "pwn.txt").exists()
    # Legitimate workspace work is unaffected.
    assert "WS_OK" in out
    assert (workspace / "candidate.txt").read_text().strip() == "ok"


@requires_sandbox
def test_generate_does_not_wrap_when_cwd_is_none(tmp_path_factory, monkeypatch):
    """Container executors pass ``cwd=None``; the sandbox must not engage there
    (they are already externally sandboxed)."""
    agent, workspace, _ = _escape_probe(tmp_path_factory)
    agent.sandbox = hostsandbox.build(workspace, env=agent.env, binary_path=agent.binary_path)
    # cwd=None => no wrapping => stub runs from the process cwd unconfined. Run
    # from a scratch dir so the stub's ``./candidate.txt`` doesn't leak into the
    # repo, and confirm it still reaches the sibling (guard skips confinement).
    scratch = tmp_path_factory.mktemp("scratch")
    monkeypatch.chdir(scratch)
    out = agent.generate("no cwd", cwd=None, silent=True)
    assert "READ_OK" in out
