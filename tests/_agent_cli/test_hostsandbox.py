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


# CI sets ``VIBESYS_REQUIRE_SANDBOX_TESTS=1`` so the real-confinement tests must
# run instead of silently skipping. If the backend is then unavailable the tests
# fail (build() returns None → assertions trip), which is the whole point: a
# broken or absent sandbox in CI should be loud, not invisible. The force is
# scoped to the backend's own platform so the Linux CI job does not try to run
# the macOS test (and vice versa).
_REQUIRE_SANDBOX_TESTS = os.environ.get("VIBESYS_REQUIRE_SANDBOX_TESTS") == "1"
_FORCE_LINUX = _REQUIRE_SANDBOX_TESTS and sys.platform.startswith("linux")
_FORCE_MACOS = _REQUIRE_SANDBOX_TESTS and sys.platform == "darwin"

requires_sandbox = pytest.mark.skipif(
    not (_FORCE_LINUX or _bwrap_works()),
    reason="requires a working bwrap + user-namespace stack "
    "(set VIBESYS_REQUIRE_SANDBOX_TESTS=1 on Linux to force)",
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

    def test_unsupported_platform_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hostsandbox.sys, "platform", "win32")
        logs: list[str] = []
        assert hostsandbox.build(tmp_path, env={}, log=logs.append) is None
        assert any("no host confinement backend" in m for m in logs)

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


class TestInstallRoot:
    """Regression for a real breakage caught by running codex under the sandbox:
    an npm-packaged CLI must be able to reach its sibling platform binary."""

    def test_node_package_binds_whole_package_tree(self):
        launcher = Path(
            "/home/u/.nvm/versions/node/v24/lib/node_modules/@openai/codex/bin/codex.js"
        )
        # Must expose the dir holding node_modules, so the sibling
        # @openai/codex-linux-x64 platform binary resolves.
        root = hostsandbox._install_root(launcher)
        assert root == Path("/home/u/.nvm/versions/node/v24/lib")
        platform_bin = Path(
            "/home/u/.nvm/versions/node/v24/lib/node_modules/@openai/"
            "codex/node_modules/@openai/codex-linux-x64/bin/codex"
        )
        assert platform_bin.is_relative_to(root)

    def test_plain_binary_binds_its_directory(self):
        assert hostsandbox._install_root(Path("/opt/tool/bin/agent")) == Path("/opt/tool/bin")


def _has_pair(argv: list[str], flag: str, *operands: str) -> bool:
    for i, tok in enumerate(argv):
        if tok == flag and argv[i + 1 : i + 1 + len(operands)] == list(operands):
            return True
    return False


# ---------------------------------------------------------------------------
# macOS Seatbelt backend (runs everywhere via monkeypatched platform)
# ---------------------------------------------------------------------------


class TestMacosBuild:
    def _patch(self, monkeypatch, sandbox_exec="/usr/bin/sandbox-exec"):
        monkeypatch.setattr(hostsandbox.sys, "platform", "darwin")
        monkeypatch.setattr(hostsandbox.shutil, "which", lambda *a, **k: sandbox_exec)

    def test_build_returns_seatbelt_on_darwin(self, tmp_path, monkeypatch):
        self._patch(monkeypatch)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        sb = hostsandbox.build(workspace, env={})
        assert isinstance(sb, hostsandbox.SeatbeltSandbox)
        assert sb.workspace == workspace.resolve()

    def test_missing_sandbox_exec_returns_none(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, sandbox_exec=None)
        logs: list[str] = []
        assert hostsandbox.build(tmp_path, env={}, log=logs.append) is None
        assert any("sandbox-exec" in m for m in logs)

    def test_allow_ancestor_rejected_on_darwin(self, tmp_path, monkeypatch):
        self._patch(monkeypatch)
        workspace = tmp_path / "exp_env" / "run" / "workspace"
        workspace.mkdir(parents=True)
        sb = hostsandbox.build(workspace, env={hostsandbox.ALLOW_ENV: str(tmp_path)})
        assert isinstance(sb, hostsandbox.SeatbeltSandbox)
        assert tmp_path.resolve() not in sb.read_paths


class TestSeatbeltProfile:
    def _sandbox(self, workspace: Path) -> hostsandbox.SeatbeltSandbox:
        return hostsandbox.SeatbeltSandbox(
            sandbox_exec_path="/usr/bin/sandbox-exec",
            workspace=workspace,
            read_paths=(Path("/opt/toolchain"),),
            write_paths=(Path("/home/u/.codex"),),
        )

    def test_profile_write_confines_but_reads_broadly(self, tmp_path):
        prof = self._sandbox(tmp_path).profile()
        assert prof.startswith("(version 1)\n(deny default)")
        # Reads are broad (so the toolchain launches); the workspace is writable;
        # executable mapping and network are permitted.
        assert "(allow file-read*)" in prof
        assert "(allow file-map-executable)" in prof
        assert "(allow file-write*" in prof
        assert f'(subpath "{tmp_path}")' in prof
        assert "(allow network*)" in prof

    def test_profile_blinds_sibling_run_area(self, tmp_path):
        """The run-container tree is denied (read+write); the workspace is carved
        back out so sibling runs are hidden but the workspace still works."""
        workspace = tmp_path / "exp_env" / "run-A" / "workspace"
        workspace.mkdir(parents=True)
        sb = self._sandbox(workspace)
        prof = sb.profile()
        # The run and run-container dirs are denied...
        assert "(deny file-read* file-write*" in prof
        blind = {str(r) for r in sb.blind_roots()}
        assert str(workspace.parent) in blind  # exp_env/run-A
        assert str(workspace.parent.parent) in blind  # exp_env
        for r in blind:
            assert f'(subpath "{r}")' in prof
        # ...and the workspace itself is re-allowed after the deny.
        deny_idx = prof.index("(deny file-read* file-write*")
        reallow = f'(allow file-read* file-write* (subpath "{workspace}"))'
        assert reallow in prof
        assert prof.index(reallow) > deny_idx

    def test_blind_roots_skip_system_dirs(self, tmp_path):
        """A shallow workspace must never blind the filesystem root or a system
        dir (that would break the toolchain)."""
        sb = hostsandbox.SeatbeltSandbox(
            sandbox_exec_path="/usr/bin/sandbox-exec", workspace=Path("/tmp/ws")
        )
        # parent is /tmp, grandparent is / — root is skipped; no system root leaks.
        for r in sb.blind_roots():
            assert r != Path("/")
            assert not str(r).startswith("/usr")

    def test_wrap_shape(self, tmp_path):
        sb = self._sandbox(tmp_path)
        argv = sb.wrap(["codex", "exec", "--json"])
        assert argv[0] == "/usr/bin/sandbox-exec"
        assert argv[1] == "-p"
        assert argv[2] == sb.profile()
        assert argv[3:] == ["codex", "exec", "--json"]

    def test_sbpl_string_escapes_quotes_and_backslashes(self):
        assert hostsandbox._sbpl_string(r'/a/"b"\c') == r'"/a/\"b\"\\c"'

    def test_profile_embeds_paths_safely(self, tmp_path):
        """A workspace path with SBPL-significant characters stays quoted."""
        workspace = tmp_path / 'weird ")name'
        workspace.mkdir()
        sb = hostsandbox.SeatbeltSandbox(
            sandbox_exec_path="/usr/bin/sandbox-exec", workspace=workspace
        )
        prof = sb.profile()
        # The literal appears escaped, and the raw unescaped form does not leak.
        assert hostsandbox._sbpl_string(str(workspace)) in prof
        assert f'(subpath "{workspace}")' not in prof


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


def _seatbelt_works() -> bool:
    """True on macOS with a usable ``sandbox-exec``."""
    if sys.platform != "darwin":
        return False
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return False
    probe = "/usr/bin/true" if Path("/usr/bin/true").exists() else "/bin/true"
    try:
        proc = subprocess.run(
            [sandbox_exec, "-p", "(version 1)(allow default)", probe],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


requires_seatbelt = pytest.mark.skipif(
    not (_FORCE_MACOS or _seatbelt_works()),
    reason="requires macOS sandbox-exec (set VIBESYS_REQUIRE_SANDBOX_TESTS=1 on macOS to force)",
)


@requires_seatbelt
def test_seatbelt_blocks_escape_but_allows_workspace(tmp_path_factory):
    """macOS counterpart of the Linux regression: the Seatbelt profile denies the
    sibling read/write while the workspace stays usable."""
    agent, workspace, sibling = _escape_probe(tmp_path_factory)
    agent.sandbox = hostsandbox.build(workspace, env=agent.env, binary_path=agent.binary_path)
    assert isinstance(agent.sandbox, hostsandbox.SeatbeltSandbox)

    # Sanity-check that the profile lets an ordinary binary launch at all before
    # asserting on the escape. A dyld-startup denial shows up as an abort with no
    # stderr, so surface sandbox-exec's own output to make such failures legible.
    probe = subprocess.run(agent.sandbox.wrap(["/usr/bin/true"]), capture_output=True, text=True)
    assert probe.returncode == 0, (
        f"profile blocks a trivial launch (rc={probe.returncode}); "
        f"stderr={probe.stderr!r}\n--- profile ---\n{agent.sandbox.profile()}"
    )

    out = agent.generate("stay in the workspace", cwd=str(workspace), silent=True)

    assert "READ_OK" not in out and "SECRET=leak" not in out
    assert "WRITE_OK" not in out
    assert not (sibling / "pwn.txt").exists()
    assert "WS_OK" in out
    assert (workspace / "candidate.txt").read_text().strip() == "ok"
