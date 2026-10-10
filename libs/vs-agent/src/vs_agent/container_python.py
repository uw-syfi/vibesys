"""The one interpreter and import roots of every VibeSys process in an agent container.

A tool server that VibeSys starts in the agent container (the evaluation
servers, the profiler servers) must not depend on whatever ``python`` the base
image happens to provide: a ROCm image's is conda Python 3.10, an Ubuntu base
has none, and none of them can import the framework. The agent image installs a
uv-managed Python at a fixed path, and every such server is started with that
interpreter and with the framework's source roots on its ``PYTHONPATH``.
:func:`containerize_server` is the only place that rule is applied.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_agent.contracts import MCPServerSpec

CONTAINER_FRAMEWORK_ROOT = "/opt/vibesys"
"""Where an agent container that runs a CLI provider mounts the framework root, read-only."""

CONTAINER_PYTHON_HOME = "/opt/vibesys-python"
"""The virtual environment the agent image builds for VibeSys's own processes."""

CONTAINER_PYTHON = f"{CONTAINER_PYTHON_HOME}/bin/python"
"""The interpreter every VibeSys-owned process in an agent container runs on."""

CONTAINER_PYTHON_VERSION = "3.12"
"""The Python the agent image installs for :data:`CONTAINER_PYTHON`."""

CONTAINER_SERVER_PACKAGES: tuple[str, ...] = (
    "vs-async-ops",
    "vs-evaluation",
    "vs-evaluator-protocol",
    "vs-mcp",
    "vs-project",
    "vs-runtime",
    "vs-sandbox",
    "vs-slurm",
)
"""The framework packages a tool server in the container may import, by directory under ``libs``.

The container interpreter holds only the standard library, ``pydantic`` and
``mcp``, so each of these must import with nothing more (a test pins that).
``vs_runtime`` is reached only through its dependency-free profiler modules.
"""

_INTERPRETER_NAMES = frozenset({"python", "python3"})
_PYTHON_PATH = "PYTHONPATH"


def container_import_roots(framework_root: str = CONTAINER_FRAMEWORK_ROOT) -> tuple[str, ...]:
    """The source roots of :data:`CONTAINER_SERVER_PACKAGES` under a mounted *framework_root*."""
    return tuple(f"{framework_root}/libs/{name}/src" for name in CONTAINER_SERVER_PACKAGES)


def containerize_server(spec: MCPServerSpec) -> MCPServerSpec:
    """Return *spec* as it must be started in an agent container.

    A Python server (command ``python`` or ``python3``) runs on
    :data:`CONTAINER_PYTHON` with :func:`container_import_roots` as its
    ``PYTHONPATH``, which travels on the server because a provider may start it
    with a scrubbed environment. Any other command is returned unchanged.

    Raises:
        ValueError: *spec* sets ``PYTHONPATH`` itself; the roots belong to the launcher.
    """
    if spec.command not in _INTERPRETER_NAMES:
        return spec
    names = {key for key, _ in (*spec.env, *spec.runtime_env)}
    if _PYTHON_PATH in names:
        message = f"MCP server {spec.name!r} sets {_PYTHON_PATH}; the container launcher owns it"
        raise ValueError(message)
    return replace(
        spec,
        command=CONTAINER_PYTHON,
        env=(*spec.env, (_PYTHON_PATH, os.pathsep.join(container_import_roots()))),
    )
