"""A Slurm GPU job's PATH lacks the CUDA toolkit; the agent is told a command that works (#2117).

A job inherits the host's environment, whose ``PATH`` usually does not list
``/usr/local/cuda/bin`` or an Nsight directory, so a bare ``ncu`` exits 127 in a job. The
environment finds the tool where it is and says so; the prompt names that absolute path.
"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.prompts import PROMPTS_DIR, render_template
from vs_runtime.api.infrastructure import (
    SlurmEnvironment,
    ToolSite,
    find_job_tool,
    off_path_tools,
)
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_TOOLS = ("ncu", "nsys")
_OPERATOR = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
shell_command = ["bash", "-c"]

[vibesys.agent_gpu]
partitions = ["main"]
max_gpus = 4
max_time_minutes = 90
"""


def _executable(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@settings(max_examples=examples(pr=10, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    on_path=st.booleans(),
    in_toolkit=st.booleans(),
    versions=st.lists(st.integers(1, 20), min_size=1, max_size=3, unique=True),
    tool=st.sampled_from(_TOOLS),
)
def test_a_tool_is_named_by_the_command_that_works_in_a_job(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    on_path: bool,
    in_toolkit: bool,
    versions: list[int],
    tool: str,
) -> None:
    root = tmp_path_factory.mktemp("job-tools")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    toolkits = [root / f"cuda-{version}" / "bin" for version in versions]
    if on_path:
        _executable(bin_dir, tool)
    if in_toolkit:
        for toolkit in toolkits:
            _executable(toolkit, tool)
    path_env = os.pathsep.join(["", str(bin_dir)])
    patterns = (str(root / "cuda-*" / "bin"),)

    command = find_job_tool(tool, path_env=path_env, toolkit_patterns=patterns)

    if on_path:
        assert command == tool
    elif in_toolkit:
        assert command == str(root / f"cuda-{max(versions)}" / "bin" / tool)
        assert os.access(command, os.X_OK)
    else:
        assert command is None
    off_path = dict(off_path_tools(_TOOLS, path_env=path_env, toolkit_patterns=patterns))
    assert (tool in off_path) is (in_toolkit and not on_path)


def test_the_slurm_agent_gpu_environment_resolves_a_tool_the_job_path_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(tmp_path / "bin", "ncu")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    operator = tmp_path / "operator.toml"
    operator.write_text(_OPERATOR, encoding="utf-8")

    environment = SlurmEnvironment(load_slurm_operator_settings(operator))

    location = environment.locate_tool("ncu", ToolSite.GPU_WORKLOAD)
    assert location.available
    assert location.command == "ncu"


def test_the_notes_name_the_absolute_path_of_a_tool_the_job_path_lacks() -> None:
    facts = SimpleNamespace(
        max_gpus=4,
        max_time_minutes=90,
        read_only_paths=(),
        tool_paths=(("ncu", "/usr/local/cuda/bin/ncu"),),
    )
    bare = SimpleNamespace(max_gpus=4, max_time_minutes=90, read_only_paths=(), tool_paths=())

    def notes(agent_gpu: SimpleNamespace) -> str:
        return render_template(
            "slurm/prompt_notes.j2",
            template_dir=PROMPTS_DIR / "environments",
            service_command="",
            evaluation_tool=True,
            gpu_client="vibesys-gpu",
            gates=(),
            agent_gpu=agent_gpu,
            read_only_paths=[],
        )

    named = notes(facts)
    assert "`ncu` is `/usr/local/cuda/bin/ncu`" in named
    assert "vibesys-gpu -- /usr/local/cuda/bin/ncu" in named
    assert "exits 127" in named
    assert "exits 127" not in notes(bare)
