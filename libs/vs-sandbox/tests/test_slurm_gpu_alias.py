"""The deprecated ``slurm-gpu`` file translates faithfully, or is rejected by name."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    SlurmGpuAliasError,
    load_slurm_gpu_alias_settings,
    load_slurm_operator_settings,
    render_slurm_operator_toml,
    translate_slurm_gpu,
)
from vs_slurm.api import SlurmLocalTransport

_STAGE = "/shared/stage"
_name = st.from_regex(r"[a-z][a-z0-9]{0,6}", fullmatch=True)
_prefixes = st.lists(st.from_regex(r"[a-z][a-z0-9/]{0,6}", fullmatch=True), max_size=2).map(tuple)


@st.composite
def _tables(draw: st.DrawFn) -> dict[str, object]:
    max_gpus = draw(st.integers(1, 8))
    max_time = draw(st.integers(1, 600))
    prefix = list(draw(_prefixes))
    table: dict[str, object] = {
        "partitions": draw(st.lists(_name, min_size=1, max_size=3, unique=True)),
        "max_gpus": max_gpus,
        "max_time_minutes": max_time,
        "default_time_minutes": draw(st.integers(1, max_time)),
        "gate_gpus": draw(st.integers(1, max_gpus)),
        "gate_time_minutes": draw(st.integers(1, max_time)),
        "srun_command": [*prefix, "/usr/bin/srun"],
        "scancel_command": [*prefix, "scancel"],
        "srun_arguments": draw(st.lists(st.just("--account=lab"), max_size=1)),
    }
    if draw(st.booleans()):
        table["windows_command"] = ["slurm-windows", "--json"]
    return table


@given(table=_tables(), task_gpus=st.none() | st.integers(1, 8))
def test_the_translation_keeps_the_limits_and_sizes_the_gates(
    table: dict[str, object], task_gpus: int | None
) -> None:
    max_gpus = table["max_gpus"]
    assert isinstance(max_gpus, int)
    if task_gpus is not None and task_gpus > max_gpus:
        with pytest.raises(SlurmGpuAliasError, match=r"slurm_gpu\.max_gpus"):
            translate_slurm_gpu(table, task_gpus=task_gpus, stage_root=_STAGE)
        return

    result = translate_slurm_gpu(table, task_gpus=task_gpus, stage_root=_STAGE)

    agent = result.agent_gpu
    assert agent is not None
    assert agent.partitions == tuple(table["partitions"])  # type: ignore[arg-type]
    assert (agent.max_gpus, agent.max_time_minutes) == (max_gpus, table["max_time_minutes"])
    assert agent.default_time_minutes == table["default_time_minutes"]
    assert agent.srun_command == tuple(table["srun_command"])  # type: ignore[arg-type]
    assert agent.windows_command == (
        tuple(table["windows_command"]) if "windows_command" in table else None  # type: ignore[arg-type]
    )
    gates = result.config.sbatch_arguments
    gpus = table["gate_gpus"] if task_gpus is None else task_gpus
    assert f"--gres=gpu:{gpus}" in gates
    assert f"--time={table['gate_time_minutes']}" in gates
    assert f"--partition={','.join(table['partitions'])}" in gates  # type: ignore[arg-type]
    assert set(table["srun_arguments"]) <= set(gates)  # type: ignore[arg-type]
    transport = result.config.transport
    assert isinstance(transport, SlurmLocalTransport)
    wrapper = tuple(table["srun_command"][:-1])  # type: ignore[index]
    assert transport.shell_command == (*wrapper, "bash", "-c")
    assert result.config.remote_workspace_root == _STAGE


@settings(max_examples=25, deadline=None)
@given(table=_tables())
def test_a_translated_file_round_trips_through_the_operator_file_format(
    table: dict[str, object],
) -> None:
    result = translate_slurm_gpu(table, task_gpus=None, stage_root=_STAGE)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "slurm.toml"
        path.write_text(render_slurm_operator_toml(result), encoding="utf-8")
        assert load_slurm_operator_settings(path) == result


@settings(max_examples=25, deadline=None)
@given(table=_tables(), key=st.from_regex(r"x[a-z]{2,8}", fullmatch=True))
def test_an_unknown_key_is_rejected_naming_it(table: dict[str, object], key: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "slurm-gpu.toml"
        lines = ["[slurm_gpu]"]
        for name, value in {**table, key: 1}.items():
            rendered = (
                "[" + ", ".join(f'"{item}"' for item in value) + "]"
                if isinstance(value, list)
                else str(value)
            )
            lines.append(f"{name} = {rendered}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(SlurmGpuAliasError, match=rf"slurm_gpu\.{key}"):
            load_slurm_gpu_alias_settings(path, task_gpus=None, stage_root=_STAGE)


_BASE: dict[str, object] = {"partitions": ["main"], "max_gpus": 4, "max_time_minutes": 60}


@pytest.mark.parametrize(
    ("override", "key"),
    [
        ({"srun_command": ["wrap", "run"]}, "srun_command"),
        ({"scancel_command": ["wrap", "cancel"]}, "scancel_command"),
        ({"srun_command": ["a", "srun"], "scancel_command": ["b", "scancel"]}, "scancel_command"),
    ],
)
def test_a_wrapper_that_cannot_be_carried_over_is_rejected_naming_the_key(
    override: dict[str, object], key: str
) -> None:
    with pytest.raises(SlurmGpuAliasError, match=rf"slurm_gpu\.{key}"):
        translate_slurm_gpu({**_BASE, **override}, task_gpus=None, stage_root=_STAGE)


def test_an_error_does_not_echo_the_values_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "slurm-gpu.toml"
    path.write_text(
        '[slurm_gpu]\npartitions = ["main"]\nmax_gpus = 8\nmax_time_minutes = 60\n'
        'account = "secret-account"\n'
    )
    with pytest.raises(SlurmGpuAliasError) as error:
        load_slurm_gpu_alias_settings(path, task_gpus=None, stage_root=_STAGE)
    assert "secret-account" not in str(error.value)
