"""Docker configuration registries for CLI agent providers."""

from __future__ import annotations

from pathlib import Path

# Per-provider environment variables to set inside the container.  Used as
# the canonical "supported with --docker" registry — providers absent from
# this dict are rejected up front in ``build_agent_runner``.
#
# Claude Code refuses ``--dangerously-skip-permissions`` when running as
# root unless ``IS_SANDBOX=1`` is set, so we set it here.  We run everything
# as root inside the container (the default) to avoid uv/pip permission
# errors when the agent installs packages.
# Every provider also gets ``PYTHONPATH=/opt/vibeserve`` so the in-container
# CLI can spawn ``python -m vibeserve_agent.loops.plain.mcp_server`` against the
# bind-mounted project root (added in ``DockerSandbox.start`` for all four
# CLI providers). Without this the MCP server module wouldn't be importable
# inside the container.
DOCKER_PROVIDER_ENV: dict[str, dict[str, str]] = {
    "claude": {"IS_SANDBOX": "1", "PYTHONPATH": "/opt/vibeserve"},
    "gemini": {"PYTHONPATH": "/opt/vibeserve"},
    "codex": {"PYTHONPATH": "/opt/vibeserve"},
    "opencode": {"PYTHONPATH": "/opt/vibeserve"},
}


# Bash one-liners run inside the container at start() time, per provider.
# Each list runs sequentially; a non-zero exit at any step raises RuntimeError.
#
# Every provider gets the python3 + ``mcp`` install at the end so that the
# in-container CLI can spawn ``python -m vibeserve_agent.loops.plain.mcp_server``
# as a stdio MCP child (via the per-provider config installed by the
# active ``CodingAgent.install_mcp_servers`` hook). The default base image
# ``nvcr.io/nvidia/pytorch:25.04-py3`` already ships python3 + pip + a
# compatible ``mcp`` install, so this is a defensive no-op for the default
# image but keeps the install resilient on alternative images.
# Retry apt-get up to 5x with backoff — Ubuntu archive mirrors regularly
# return transient "connection timed out" / "mirror sync in progress"
# errors that fail a single-shot `apt-get update`.
def _apt_install(pkgs: str, check_bin: str | None = None) -> str:
    bin_ = check_bin or pkgs.split()[0]
    return (
        f"command -v {bin_} >/dev/null || "
        "{ for i in 1 2 3 4 5; do "
        f"  apt-get update -qq && apt-get install -y -qq {pkgs} && break || "
        "  (echo \"apt retry $i...\" >&2; sleep $((i*5))); "
        "done; "
        f"command -v {bin_} >/dev/null; }}"
    )


# Tarball install for node/npm — apt-get against archive.ubuntu.com is
# unreliable from inside several of our hosts (intermittent connection
# timeouts). nodejs.org / Cloudflare-fronted endpoints reach reliably.
_NODE_TARBALL_INSTALL = (
    "command -v node >/dev/null || { set -e; "
    "V=v20.18.1; A=linux-x64; "
    "cd /tmp && "
    "curl -fsSL --retry 5 --retry-delay 5 -o node.tgz "
    "  \"https://nodejs.org/dist/$V/node-$V-$A.tar.gz\" && "
    "mkdir -p /opt/node && "
    "tar -xzf node.tgz -C /opt/node --strip-components=1 && "
    "ln -sf /opt/node/bin/node /usr/local/bin/node && "
    "ln -sf /opt/node/bin/npm /usr/local/bin/npm && "
    "ln -sf /opt/node/bin/npx /usr/local/bin/npx && "
    "/opt/node/bin/npm config set prefix /usr/local && "
    "rm -f node.tgz; }"
)


_MCP_PYTHON_INSTALL = [
    _apt_install("python3 python3-pip"),
    "python3 -m pip install --quiet 'mcp>=1.0'",
]

_DOCKER_INSTALL_COMMANDS: dict[str, list[str]] = {
    "claude": [
        _apt_install("curl ca-certificates", check_bin="curl"),
        "curl -fsSL https://claude.ai/install.sh | bash",
        # Anthropic's installer drops the binary in /root/.local/bin —
        # symlink to /usr/local/bin so PATH doesn't need adjustment.
        "ln -sf /root/.local/bin/claude /usr/local/bin/claude",
        *_MCP_PYTHON_INSTALL,
    ],
    "opencode": [
        _apt_install("curl ca-certificates", check_bin="curl"),
        "curl -fsSL https://opencode.ai/install | bash",
        "ln -sf /root/.opencode/bin/opencode /usr/local/bin/opencode 2>/dev/null || "
        "ln -sf /root/.local/bin/opencode /usr/local/bin/opencode",
        *_MCP_PYTHON_INSTALL,
    ],
    "gemini": [
        _NODE_TARBALL_INSTALL,
        "npm install -g @google/gemini-cli",
        *_MCP_PYTHON_INSTALL,
    ],
    "codex": [
        _NODE_TARBALL_INSTALL,
        # Pin to >=0.125.0 so the gpt-5.5 family of models works (0.118
        # rejects them with 'requires a newer version of Codex').
        # `--include=optional` because newer codex packages ship the
        # Linux-x64 native binary as an optional dependency that
        # `npm install -g` silently skips on some npm configurations.
        "npm install -g --include=optional @openai/codex@0.125.0",
        *_MCP_PYTHON_INSTALL,
    ],
}


def docker_init_commands(provider: str) -> list[str]:
    """Return the list of init commands for *provider*."""
    return list(_DOCKER_INSTALL_COMMANDS.get(provider, []))


# Host paths to bind-mount RW so the in-container CLI inherits the host's
# login state.  Only entries that actually exist on the host are mounted.
# Claude paths are well-tested; the other three are best-guesses that may
# need adjustment after first real run.
DOCKER_AUTH_PATHS: dict[str, list[Path]] = {
    "claude": [Path.home() / ".claude", Path.home() / ".claude.json"],
    "gemini": [Path.home() / ".gemini"],
    "codex": [Path.home() / ".codex"],
    "opencode": [
        Path.home() / ".local" / "share" / "opencode",
        Path.home() / ".config" / "opencode",
    ],
}


def auth_bind_mounts(provider: str) -> list[tuple[str, str, bool]]:
    """Return ``(host_path, container_path, readonly=False)`` for existing auth paths.

    Auth dirs are mounted into ``/root`` since we run as root inside the
    container.
    """
    out: list[tuple[str, str, bool]] = []
    for host in DOCKER_AUTH_PATHS.get(provider, []):
        if host.exists():
            out.append((str(host), "/root/" + host.name, False))
    return out
