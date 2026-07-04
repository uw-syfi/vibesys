"""Tests for per-provider MCP server install/uninstall on the CLI agents.

Each ``CodingAgent`` subclass owns the knowledge of how to surface a stdio
MCP server to its CLI tool. These tests directly exercise that
provider-specific logic against a ``tmp_path`` workspace and a synthetic
:class:`MCPServerSpec`, asserting:

- the right config file is created at the right path,
- the file contents follow the provider's documented schema,
- ``uninstall_mcp_servers`` removes the file (idempotently),
- the Codex provider writes **no** file and instead stores ``--config``
  flags on ``self.extra_config_args``.

Network/binary access is not required: nothing here actually launches the
CLIs, only inspects what each agent writes to disk (or to the instance).
"""

from __future__ import annotations

import json
from pathlib import Path

from vibe_serve._agent_cli.base import MCPServerSpec
from vibe_serve._agent_cli.claude import ClaudeCodeCodingAgent
from vibe_serve._agent_cli.codex import CodexCodingAgent
from vibe_serve._agent_cli.gemini import GeminiCodingAgent
from vibe_serve._agent_cli.opencode import OpencodeCodingAgent


def _spec() -> MCPServerSpec:
    return MCPServerSpec(
        name="vibeserve-issues",
        command="python",
        args=[
            "-m",
            "vs_issue_board.mcp",
            "issues.json",
            "--creator",
            "judge",
            "--iteration",
            "3",
            "--allowed-types",
            "bug",
            "--cap",
            "1",
        ],
    )


def _spec_with_env() -> MCPServerSpec:
    return MCPServerSpec(
        name="vibeserve-issues",
        command="python",
        args=["-m", "vs_issue_board.mcp", "issues.json"],
        env={"MY_VAR": "my_value", "OTHER": "x"},
    )


# ---------------------------------------------------------------------------
# Claude Code → workspace/.mcp.json
# ---------------------------------------------------------------------------


class TestClaudeMCP:
    def _agent(self):
        # Avoid binary lookup by skipping the normal init: we only call
        # install/uninstall, which doesn't touch the binary.
        return ClaudeCodeCodingAgent.__new__(ClaudeCodeCodingAgent)

    def test_install_writes_mcp_json(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])

        target = tmp_path / ".mcp.json"
        assert target.exists()
        config = json.loads(target.read_text())
        server = config["mcpServers"]["vibeserve-issues"]
        assert server["command"] == "python"
        assert server["args"] == _spec().args
        assert "trust" not in server
        assert "env" not in server

    def test_install_includes_env_when_present(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec_with_env()])

        config = json.loads((tmp_path / ".mcp.json").read_text())
        server = config["mcpServers"]["vibeserve-issues"]
        assert server["env"] == {"MY_VAR": "my_value", "OTHER": "x"}

    def test_uninstall_removes_file(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])
        assert (tmp_path / ".mcp.json").exists()

        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        assert not (tmp_path / ".mcp.json").exists()

    def test_uninstall_is_idempotent(self, tmp_path: Path):
        agent = self._agent()
        # No file present yet → no error.
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        # And again.
        agent.uninstall_mcp_servers(tmp_path, [_spec()])


# ---------------------------------------------------------------------------
# Gemini → workspace/.gemini/settings.json
# ---------------------------------------------------------------------------


class TestGeminiMCP:
    def _agent(self):
        return GeminiCodingAgent.__new__(GeminiCodingAgent)

    def test_install_writes_settings_json_with_trust(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])

        target = tmp_path / ".gemini" / "settings.json"
        assert target.exists()
        assert (tmp_path / ".gemini").is_dir()
        config = json.loads(target.read_text())
        server = config["mcpServers"]["vibeserve-issues"]
        assert server["command"] == "python"
        assert server["args"] == _spec().args
        # trust:true skips Gemini's per-tool approval prompts.
        assert server["trust"] is True

    def test_install_creates_gemini_dir_if_missing(self, tmp_path: Path):
        agent = self._agent()
        assert not (tmp_path / ".gemini").exists()
        agent.install_mcp_servers(tmp_path, [_spec()])
        assert (tmp_path / ".gemini").is_dir()

    def test_install_with_existing_gemini_dir(self, tmp_path: Path):
        # Mimic a workspace where Gemini already wrote tmp/ session data.
        (tmp_path / ".gemini" / "tmp").mkdir(parents=True)
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])
        assert (tmp_path / ".gemini" / "settings.json").exists()
        assert (tmp_path / ".gemini" / "tmp").is_dir()  # untouched

    def test_uninstall_removes_settings_but_keeps_dir(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        assert not (tmp_path / ".gemini" / "settings.json").exists()
        assert (tmp_path / ".gemini").is_dir()

    def test_uninstall_is_idempotent(self, tmp_path: Path):
        agent = self._agent()
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        agent.uninstall_mcp_servers(tmp_path, [_spec()])

    def test_install_includes_env_when_present(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec_with_env()])
        config = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
        server = config["mcpServers"]["vibeserve-issues"]
        assert server["env"] == {"MY_VAR": "my_value", "OTHER": "x"}


# ---------------------------------------------------------------------------
# Opencode → workspace/opencode.json
# ---------------------------------------------------------------------------


class TestOpencodeMCP:
    def _agent(self):
        return OpencodeCodingAgent.__new__(OpencodeCodingAgent)

    def test_install_writes_opencode_json(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])

        target = tmp_path / "opencode.json"
        assert target.exists()
        config = json.loads(target.read_text())
        # opencode uses 'mcp' (not 'mcpServers')
        assert "mcp" in config
        assert "mcpServers" not in config
        assert config["$schema"] == "https://opencode.ai/config.json"

        server = config["mcp"]["vibeserve-issues"]
        assert server["type"] == "local"
        assert server["enabled"] is True
        # opencode uses a single combined command array.
        assert server["command"] == ["python", *_spec().args]

    def test_install_uses_environment_key_for_env(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec_with_env()])
        config = json.loads((tmp_path / "opencode.json").read_text())
        server = config["mcp"]["vibeserve-issues"]
        # opencode's key is 'environment', not 'env'.
        assert server["environment"] == {"MY_VAR": "my_value", "OTHER": "x"}
        assert "env" not in server

    def test_uninstall_removes_file(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        assert not (tmp_path / "opencode.json").exists()

    def test_uninstall_is_idempotent(self, tmp_path: Path):
        agent = self._agent()
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        agent.uninstall_mcp_servers(tmp_path, [_spec()])


# ---------------------------------------------------------------------------
# Codex → no file; --config flags on the instance
# ---------------------------------------------------------------------------


class TestCodexMCP:
    def _agent(self):
        agent = CodexCodingAgent.__new__(CodexCodingAgent)
        agent.base_config_args = []
        agent.extra_config_args = []
        return agent

    def test_install_writes_no_file_in_workspace(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])
        assert list(tmp_path.iterdir()) == []

    def test_install_populates_extra_config_args(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])

        # We expect pairs of "--config" + "key=value" entries.
        flags = agent.extra_config_args
        # Strip the "--config" sentinels and inspect the value strings.
        values = [v for f, v in zip(flags[0::2], flags[1::2], strict=True) if f == "--config"]
        assert all(f == "--config" for f in flags[0::2])

        # name "vibeserve-issues" should snake-case to "vibeserve_issues".
        joined = "\n".join(values)
        assert 'mcp_servers.vibeserve_issues.command="python"' in joined
        # The args= entry should be a TOML array of quoted strings.
        args_entries = [v for v in values if v.startswith("mcp_servers.vibeserve_issues.args=")]
        assert len(args_entries) == 1
        args_value = args_entries[0].split("=", 1)[1]
        assert args_value.startswith("[") and args_value.endswith("]")
        assert '"-m"' in args_value
        assert '"vs_issue_board.mcp"' in args_value
        assert '"--creator"' in args_value
        assert '"judge"' in args_value
        assert '"--cap"' in args_value
        assert '"1"' in args_value

    def test_install_includes_env_overrides(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec_with_env()])
        joined = "\n".join(agent.extra_config_args)
        assert 'mcp_servers.vibeserve_issues.env.MY_VAR="my_value"' in joined
        assert 'mcp_servers.vibeserve_issues.env.OTHER="x"' in joined

    def test_install_quotes_strings_with_special_characters(self, tmp_path: Path):
        agent = self._agent()
        spec = MCPServerSpec(
            name="weird",
            command='py"thon',
            args=["back\\slash", 'quoted"value'],
        )
        agent.install_mcp_servers(tmp_path, [spec])
        joined = "\n".join(agent.extra_config_args)
        # Backslashes and quotes get escaped per TOML basic-string rules.
        assert 'mcp_servers.weird.command="py\\"thon"' in joined
        assert '"back\\\\slash"' in joined
        assert '"quoted\\"value"' in joined

    def test_uninstall_clears_extra_config_args(self, tmp_path: Path):
        agent = self._agent()
        agent.install_mcp_servers(tmp_path, [_spec()])
        assert agent.extra_config_args  # populated
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        assert agent.extra_config_args == []

    def test_uninstall_is_idempotent(self, tmp_path: Path):
        agent = self._agent()
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        agent.uninstall_mcp_servers(tmp_path, [_spec()])
        assert agent.extra_config_args == []

    def test_get_command_appends_extra_config_args(self):
        agent = self._agent()
        agent.binary_path = "/usr/local/bin/codex"
        agent.model = None
        # Without install: cmd is the bare exec line.
        cmd = agent._get_command("hello")
        assert "--config" not in cmd

        # After install: --config flags are appended.
        agent.install_mcp_servers(Path("/tmp"), [_spec()])
        cmd = agent._get_command("hello")
        assert "--config" in cmd
        assert any(s.startswith("mcp_servers.vibeserve_issues.command=") for s in cmd)


# ---------------------------------------------------------------------------
# Default no-op base class fallback
# ---------------------------------------------------------------------------


def test_base_class_default_install_uninstall_are_noop(tmp_path: Path):
    """A subclass that doesn't override should silently do nothing."""

    from vibe_serve._agent_cli.base import CodingAgent

    class DummyAgent(CodingAgent):
        def generate(self, prompt, cwd=None, timeout=None, silent=False):
            return ""

    agent = DummyAgent()
    # Returns None, doesn't write anything, doesn't raise.
    assert agent.install_mcp_servers(tmp_path, [_spec()]) is None
    assert agent.uninstall_mcp_servers(tmp_path, [_spec()]) is None
    assert list(tmp_path.iterdir()) == []
