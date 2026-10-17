"""A Slurm executor declares exclusive hosts exactly when its jobs hold their nodes alone."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_slurm.api import SlurmConfig, SlurmSshTransport

_SHARING = ("--exclusive", "--exclusive=user", "--exclusive=mcs", "--oversubscribe", "-s")
_OTHER = ("--gres=gpu:1", "--partition=b200", "--nodes=1", "-N", "1")


@given(arguments=st.lists(st.sampled_from(_SHARING + _OTHER), max_size=6))
def test_the_last_sharing_option_decides_and_only_a_bare_exclusive_holds_the_node(
    arguments: list[str],
) -> None:
    """sbatch takes the last of ``--exclusive[=...]`` and ``--oversubscribe`` it reads."""
    config = SlurmConfig(
        name="test",
        remote_workspace_root="/runs",
        transport=SlurmSshTransport(host="h"),
        sbatch_arguments=tuple(arguments),
    )
    sharing = [argument for argument in arguments if argument in _SHARING]
    assert config.exclusive_hosts is (bool(sharing) and sharing[-1] == "--exclusive")
