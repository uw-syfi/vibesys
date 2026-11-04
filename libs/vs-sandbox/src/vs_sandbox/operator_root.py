"""The operator's runtime root, which no agent confinement may let the agent reach.

The user's runtime root (``vs_project.api.user_runtime_root``) holds what makes a process
the operator: the operator key a run's control socket demands
(``vs_project.api.operator_key_path``), the registry of live servers and their control
sockets, and the web gateway's capability record. An agent that could read it could
present the key and steer, stop or cancel its own run, or a rival's work.

Every confinement therefore keeps its agent's view and this root disjoint. Bubblewrap,
Landlock and Docker can only grant, never subtract, so each refuses to build a view in
which a granted path contains the root or lies inside it (:func:`exposed_grant`). Seatbelt
can subtract, so its profile denies the root outright. The root is read from the process
that builds the confinement, which is the operator's: the same environment its server
and its clients find the key from.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from vs_project.api import user_runtime_root

if TYPE_CHECKING:
    from collections.abc import Iterable


def operator_root() -> Path:
    """This process's runtime root: where its servers' key and sockets live."""
    return user_runtime_root(os.environ, os.getuid())


def exposed_grant(root: Path, granted: Iterable[Path | str]) -> Path | None:
    """The first of ``granted`` that overlaps ``root``, or ``None`` when none does.

    A grant overlaps when it is the root, contains it, or lies inside it. Both sides are
    compared resolved, so a symlink to or into the root overlaps too.
    """
    resolved_root = root.resolve()
    for grant in granted:
        resolved = Path(grant).resolve()
        if (
            resolved == resolved_root
            or resolved in resolved_root.parents
            or resolved_root in resolved.parents
        ):
            return Path(grant)
    return None


def exposure_message(backend: str, root: Path, grant: Path) -> str:
    """Why ``backend`` refuses a view in which ``grant`` reaches ``root``."""
    return (
        f"{backend} would grant the agent {grant}, which reaches the operator's runtime "
        f"root {root}: it holds the key to the run's control socket, so the agent could "
        "steer or stop its own run. Point XDG_RUNTIME_DIR at a private directory outside "
        "every path the agent is granted (systemd's /run/user/<uid> is), or drop the grant."
    )
