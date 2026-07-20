from __future__ import annotations

from pathlib import Path

import pytest

from vibesys.agents import host_resource_declarations
from vs_sandbox import HostResourceAccess


class TestInstallRoot:
    """Agent packages may need binaries from sibling installation paths."""

    def test_node_package_imports_whole_package_tree(self):
        launcher = Path(
            "/home/u/.nvm/versions/node/v24/lib/node_modules/@openai/codex/bin/codex.js"
        )
        root = host_resource_declarations._install_root(launcher)

        assert root == Path("/home/u/.nvm/versions/node/v24/lib")
        platform_bin = Path(
            "/home/u/.nvm/versions/node/v24/lib/node_modules/@openai/"
            "codex/node_modules/@openai/codex-linux-x64/bin/codex"
        )
        assert platform_bin.is_relative_to(root)

    def test_plain_binary_imports_its_directory(self):
        assert host_resource_declarations._install_root(Path("/opt/tool/bin/agent")) == Path(
            "/opt/tool/bin"
        )


def test_defaults_declare_path_rust_and_shell_resources(tmp_path):
    home = tmp_path / "home"
    tool_bin = home / "tools" / "bin"
    cargo_bin = home / ".cargo" / "bin"
    rustup_home = home / ".rustup"
    bash_profile = home / ".bash_profile"

    declarations = host_resource_declarations.declare_agent_host_resources(
        {"HOME": str(home), "PATH": f"{tool_bin}:/usr/bin"},
        binary_path=None,
        provider="codex",
    )
    resources = {resource.path: resource.access for resource in declarations}

    assert resources[tool_bin] is HostResourceAccess.READ_ONLY
    assert resources[cargo_bin] is HostResourceAccess.READ_ONLY
    assert resources[rustup_home] is HostResourceAccess.READ_ONLY
    assert resources[bash_profile] is HostResourceAccess.READ_ONLY
    assert home not in resources


@pytest.mark.parametrize(
    ("provider", "expected", "forbidden"),
    [
        ("codex", ".codex/auth.json", ".claude"),
        ("claude", ".claude", ".gemini"),
        ("gemini", ".gemini", ".config/opencode"),
        ("opencode", ".config/opencode", ".codex/auth.json"),
    ],
)
def test_provider_state_is_scoped_to_selected_agent(tmp_path, provider, expected, forbidden):
    declarations = host_resource_declarations.declare_agent_host_resources(
        {"HOME": str(tmp_path)},
        binary_path=None,
        provider=provider,
    )
    writable = {
        resource.path.relative_to(tmp_path).as_posix()
        for resource in declarations
        if resource.access is HostResourceAccess.READ_WRITE
        and resource.path.is_relative_to(tmp_path)
    }

    assert expected in writable
    assert forbidden not in writable
