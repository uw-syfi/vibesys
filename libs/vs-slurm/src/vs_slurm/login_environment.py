"""Which host environment variables a local-transport process keeps: a login's."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGIN_ENVIRONMENT_NAMES = frozenset(
    {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "TZ", "TMPDIR"}
)
_LOGIN_ENVIRONMENT_PREFIX = "LC_"


def login_environment(host: Mapping[str, str]) -> dict[str, str]:
    """Return the part of *host* that a login session would also have.

    The local transport runs its programs in this environment and nothing else,
    which is what the SSH transport gets from sshd: a fresh one. A host process
    can hold credentials, and inside a Slurm allocation or batch job it holds
    ``SLURM_*`` variables that would redirect the gate's own ``sbatch`` to the
    wrong job. The names kept are the identity and locale ones a login sets
    (``HOME USER LOGNAME SHELL PATH LANG LC_* TZ``) and ``TMPDIR``; every other
    name, ``SLURM_*``, ``SBATCH_*``, ``SALLOC_*`` and ``SRUN_*`` included, is dropped.
    """
    return {
        name: value
        for name, value in host.items()
        if name in _LOGIN_ENVIRONMENT_NAMES
        or (
            name.startswith(_LOGIN_ENVIRONMENT_PREFIX)
            and len(name) > len(_LOGIN_ENVIRONMENT_PREFIX)
        )
    }


__all__ = ["login_environment"]
