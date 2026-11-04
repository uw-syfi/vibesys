"""Every tool server VibeSys starts in an agent container runs on one interpreter and one import path.

A server's descriptor names a generic ``python`` and no import roots; the agent
launcher (``containerize_server``) is the only place that picks the container's
interpreter and ``PYTHONPATH``. The container interpreter holds only the standard
library, pydantic and mcp, so each framework module a server imports has to import
with nothing more.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from launch.agents import run_tool_descriptors
from launch.composition import profiler_server
from vibesys.api.wiring import AgentToolContext
from vibesys.config import BUNDLED_RESOURCES
from vibesys.run.contracts import ProfilerKind
from vs_agent.api import (
    CONTAINER_FRAMEWORK_ROOT,
    CONTAINER_PYTHON,
    CONTAINER_SERVER_PACKAGES,
    MCPServerSpec,
    container_import_roots,
    containerize_server,
)
from vs_evaluation.api.tools import core_evaluation_mcp_descriptor
from vs_mcp.api import StdioServerDescriptor, ToolServerDescriptor
from vs_sim.api.testing import examples

_PYTHON_NAMES = ("python", "python3")
_expected_path = os.pathsep.join(container_import_roots())


def _spec(descriptor: ToolServerDescriptor) -> MCPServerSpec:
    assert isinstance(descriptor, StdioServerDescriptor)
    return MCPServerSpec(
        descriptor.name,
        descriptor.command,
        descriptor.args,
        descriptor.env,
        descriptor.runtime_env,
    )


def _launcher_descriptors() -> list[ToolServerDescriptor]:
    """What each server launcher in the product produces, for every profiler it can bind."""
    descriptors = [
        core_evaluation_mcp_descriptor("token", "/run/evaluation.sock"),
        *run_tool_descriptors("/run/relay.sock", "token"),
    ]
    for kind in ProfilerKind:
        server = profiler_server(AgentToolContext(profiler_id=kind.value))
        if server is not None:
            descriptors.append(server)
    return descriptors


def test_the_survey_covers_a_profiler_server() -> None:
    names = {descriptor.name for descriptor in _launcher_descriptors()}

    assert "vibesys-linux-cpu-profiler" in names
    assert "vs-evaluation" in names
    assert "vibesys-run" in names


@pytest.mark.parametrize("descriptor", _launcher_descriptors(), ids=lambda d: d.name)
def test_no_server_descriptor_chooses_an_interpreter_or_import_path(
    descriptor: ToolServerDescriptor,
) -> None:
    spec = _spec(descriptor)

    assert spec.command in _PYTHON_NAMES
    assert "PYTHONPATH" not in {key for key, _ in (*spec.env, *spec.runtime_env)}


@pytest.mark.parametrize("descriptor", _launcher_descriptors(), ids=lambda d: d.name)
def test_every_server_starts_in_the_container_on_the_one_interpreter_and_import_path(
    descriptor: ToolServerDescriptor,
) -> None:
    spec = _spec(descriptor)

    started = containerize_server(spec)

    assert started.command == CONTAINER_PYTHON
    assert started.args == spec.args
    assert dict(started.env) == {**dict(spec.env), "PYTHONPATH": _expected_path}
    assert started.runtime_env == spec.runtime_env


_names = st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8)
_env = st.dictionaries(_names.map(str.upper), _names, max_size=3).filter(
    lambda env: "PYTHONPATH" not in env
)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    command=st.sampled_from(_PYTHON_NAMES),
    args=st.lists(_names, max_size=3),
    env=_env,
    runtime_env=_env,
)
def test_any_python_server_gets_the_container_interpreter_and_import_path(
    command: str, args: list[str], env: dict[str, str], runtime_env: dict[str, str]
) -> None:
    runtime_env = {key: value for key, value in runtime_env.items() if key not in env}
    spec = MCPServerSpec(
        "server", command, tuple(args), tuple(env.items()), tuple(runtime_env.items())
    )

    started = containerize_server(spec)

    assert started.command == CONTAINER_PYTHON
    assert started.args == spec.args
    assert dict(started.env) == {**env, "PYTHONPATH": _expected_path}
    assert started.runtime_env == spec.runtime_env


@settings(max_examples=examples(pr=8, nightly=100))
@given(command=_names.filter(lambda name: name not in _PYTHON_NAMES), args=st.lists(_names))
def test_a_server_that_is_not_python_is_left_alone(command: str, args: list[str]) -> None:
    spec = MCPServerSpec("server", command, tuple(args))

    assert containerize_server(spec) == spec


@settings(max_examples=examples(pr=8, nightly=100))
@given(command=st.sampled_from(_PYTHON_NAMES), in_runtime=st.booleans())
def test_a_server_that_sets_its_own_import_path_is_rejected(
    *, command: str, in_runtime: bool
) -> None:
    pair = (("PYTHONPATH", "/elsewhere"),)
    spec = MCPServerSpec(
        "server", command, env=() if in_runtime else pair, runtime_env=pair if in_runtime else ()
    )

    with pytest.raises(ValueError, match="PYTHONPATH"):
        containerize_server(spec)


def test_the_import_roots_are_the_framework_mounts_package_sources() -> None:
    assert container_import_roots() == tuple(
        f"{CONTAINER_FRAMEWORK_ROOT}/libs/{name}/src" for name in CONTAINER_SERVER_PACKAGES
    )
    assert all(Path(root).name == "src" for root in container_import_roots())


# --- what the container interpreter can import -------------------------------------------

_PROBE = """\
import importlib, sys
baseline_modules = {name.split(".")[0] for name in sys.modules}
import mcp.server.fastmcp, pydantic
allowed = {name.split(".")[0] for name in sys.modules} | sys.stdlib_module_names
for module in sys.argv[1:]:
    importlib.import_module(module)
loaded = {name.split(".")[0] for name in sys.modules}
print("framework", *sorted(n for n in loaded if n.startswith("vs_")))
print("other", *sorted(n for n in loaded - allowed if not n.startswith(("vs_", "_"))))
"""


def _server_framework_imports() -> list[str]:
    """Every ``vs_*`` module a bundled profiler server file imports, as written.

    The macOS profiler is left out: it only ever runs on a macOS host, never in a container.
    """
    root = BUNDLED_RESOURCES.directory("profilers", "linux_cpu")
    assert root is not None
    modules: set[str] = set()
    for path in root.parent.glob("*/*.py"):
        if path.parent.name == "macos_cpu":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("vs_"):
                modules.add(cast("str", node.module))
    return sorted(modules)


def _launcher_entrypoint_modules() -> list[str]:
    """The module each launched server runs with ``python -m``, as its descriptor names it."""
    modules = []
    for descriptor in _launcher_descriptors():
        assert isinstance(descriptor, StdioServerDescriptor)
        if descriptor.args[:1] == ("-m",):
            modules.append(descriptor.args[1])
    return modules


def test_the_survey_covers_the_chat_agents_run_tools_and_the_evaluation_tool() -> None:
    assert {"vs_evaluation.tool_relay_mcp", "vs_evaluation.agent_core_mcp"} <= set(
        _launcher_entrypoint_modules()
    )


def test_the_profiler_servers_import_only_the_container_packages() -> None:
    modules = _server_framework_imports()

    assert "vs_runtime.cpu_profilers" in modules
    assert "vs_runtime.api.infrastructure" not in modules
    assert {name.split(".")[0] for name in modules} <= {
        name.replace("-", "_") for name in CONTAINER_SERVER_PACKAGES
    }


def test_every_module_a_container_server_imports_needs_only_the_container_interpreters_packages() -> (
    None
):
    modules = [*_launcher_entrypoint_modules(), *_server_framework_imports()]

    # A fresh interpreter is the only way to see exactly what the imports pull in.
    probe = subprocess.run(  # noqa: S603  # LW-158203; the argv is this interpreter and a fixed program.
        # > Importing in-process would see this test session's modules, not the server's own
        # > closure. A wrapper would only hide the single direct call, and shell=True would
        # > weaken the argv guarantee.
        [sys.executable, "-c", _PROBE, *modules],
        capture_output=True,
        text=True,
        check=True,
    )
    framework, other = (line.split()[1:] for line in probe.stdout.strip().splitlines())

    assert other == [], "imports beyond the stdlib, pydantic and mcp"
    assert set(framework) <= {name.replace("-", "_") for name in CONTAINER_SERVER_PACKAGES}
