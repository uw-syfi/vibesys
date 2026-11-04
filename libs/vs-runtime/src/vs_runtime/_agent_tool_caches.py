"""Where the agent container keeps the caches of the tools it runs.

A tool cache is machine state, not candidate content, so it never lives in a
candidate's workspace. Those caches hold what the tools fetched, and some of it is
a Git checkout (uv's source-distribution cache, Cargo's registry and Git
checkouts). A checkout carries its own ``.git``, which the host's Git guard
refuses as a nested repository the next time it reads that workspace, however the
workspace ignores the directory. The caches live under the agent's home instead:
one cache root per container, and each candidate has its own container.
"""

from __future__ import annotations

from importlib import import_module


def agent_tool_cache_env() -> dict[str, str]:
    """The environment that points every workspace-writing tool's cache outside the workspace.

    The home directory already hosts the tools that follow ``HOME`` or
    ``XDG_CACHE_HOME`` (pip, Hugging Face, Torch, Triton); the entries here are the
    ones the container pins to another place. The cache and the workspace are on
    different file systems, so uv copies rather than links into the environment.
    """
    # Deferred: the Docker sandbox module imports the agent stack, which this
    # module must not load.
    home = import_module("vs_sandbox.api").AGENT_HOME
    cache_root = f"{home}/.cache"
    return {
        "UV_CACHE_DIR": f"{cache_root}/uv",
        "UV_LINK_MODE": "copy",
        "CARGO_HOME": f"{cache_root}/cargo",
    }
