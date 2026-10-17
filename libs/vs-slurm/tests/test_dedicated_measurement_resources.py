"""A Slurm executor declares dedicated measurement resources by operator declaration or ``--exclusive``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_slurm.api import SlurmConfig, SlurmConfigError, SlurmSshTransport, load_slurm_config

if TYPE_CHECKING:
    from pathlib import Path

_SHARING = (
    "--exclusive",
    "--exclusive=user",
    "--exclusive=mcs",
    "--oversubscribe",
    "--share",
    "-s",
)
_OTHER = ("--gres=gpu:1", "--partition=b200", "--nodes=1", "-N", "1")


def _config(arguments: list[str], *, declared: bool | None = None) -> SlurmConfig:
    return SlurmConfig(
        name="test",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="h"),
        sbatch_arguments=tuple(arguments),
        **({} if declared is None else {"dedicated_measurement_resources": declared}),
    )


@given(
    arguments=st.lists(st.sampled_from(_SHARING + _OTHER), max_size=6),
    declared=st.none() | st.booleans(),
)
def test_a_declaration_or_a_last_bare_exclusive_dedicates_the_resources(
    arguments: list[str], *, declared: bool | None
) -> None:
    """sbatch takes the last of ``--exclusive[=...]`` and ``--oversubscribe`` it reads."""
    sharing = [argument for argument in arguments if argument in _SHARING]
    exclusive = bool(sharing) and sharing[-1] == "--exclusive"
    assert _config(arguments, declared=declared).dedicates_measurement_resources is (
        bool(declared) or exclusive
    )


def test_the_declaration_alone_enables_it_on_a_shared_node() -> None:
    config = _config(["--gres=gpu:4", "-c", "64"], declared=True)
    assert config.dedicates_measurement_resources is True


def test_a_bare_exclusive_alone_enables_it() -> None:
    assert _config(["--exclusive"]).dedicates_measurement_resources is True


def test_neither_leaves_it_false() -> None:
    assert _config(["--gres=gpu:4", "-c", "64"]).dedicates_measurement_resources is False
    assert _config([], declared=False).dedicates_measurement_resources is False


def test_the_declaration_is_read_from_the_toml_and_an_unknown_key_is_rejected(
    tmp_path: Path,
) -> None:
    table = (
        '[slurm]\nname = "c"\nremote_workspace_root = "/r"\n{extra}'
        '\n[slurm.transport]\nkind = "ssh"\nhost = "c"\n'
    )
    declared = tmp_path / "declared.toml"
    declared.write_text(table.format(extra="dedicated_measurement_resources = true\n"))
    assert load_slurm_config(declared).dedicates_measurement_resources is True

    unknown = tmp_path / "unknown.toml"
    unknown.write_text(table.format(extra="dedicated_measurement_resource = true\n"))
    with pytest.raises(SlurmConfigError, match="dedicated_measurement_resource"):
        load_slurm_config(unknown)
