"""What an editor container needs to reach a run's host command broker.

Both Slurm run environments keep the agent in a Docker container and answer its
requests on the host: GPU jobs and trusted gates for ``slurm-gpu``, trusted
gates for ``slurm``. The container receives the broker's Unix socket, the
single-file client, and the variables the client reads. The broker, its token
and the planned commands stay on the host.
"""

from __future__ import annotations

import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vs_runtime._run_environment import EditorExtras
from vs_sandbox.api import HostResource, HostResourceAccess
from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    HOST_COMMAND_CLIENT,
    GateKind,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_sandbox.api.slurm import HostCommandBroker

_SHEBANG = "#!/usr/bin/env python3\n"
#: Where the client appears in the container. Every image has it on ``PATH``,
#: including a login shell's, which resets any ``PATH`` set through the environment.
CLIENT_BIN_DIR = "/usr/local/bin"


def planned_gates(accuracy_command: str | None, benchmark_command: str | None) -> tuple[str, ...]:
    """Return the gate kinds the task plans, as the client names them."""
    planned = {GateKind.ACCURACY: accuracy_command, GateKind.BENCHMARK: benchmark_command}
    return tuple(kind.value for kind, command in planned.items() if command)


def new_broker_socket_path() -> Path:
    """Return a fresh, short socket path (Unix socket paths are length-limited)."""
    return Path(tempfile.gettempdir()) / f"vsg-{secrets.token_hex(8)}.sock"


def write_client_launcher(state_dir: Path, name: str) -> Path:
    """Write the executable single-file client as *name* in *state_dir*."""
    state_dir.mkdir(parents=True, exist_ok=True)
    launcher = state_dir / name
    launcher.write_text(_SHEBANG + HOST_COMMAND_CLIENT.read_text())
    launcher.chmod(0o755)
    return launcher


def bridge_editor_extras(
    broker: HostCommandBroker,
    launcher: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> EditorExtras:
    """Return the editor additions that bridge the container to *broker*.

    The socket (read-write) and the launcher's directory (read-only) are mounted
    at their host paths, the workspace is mounted at its host path too, and the
    container gets no accelerator: whatever it computes on, it computes through
    the broker. The launcher is also mounted under ``/usr/local/bin`` so its bare
    name resolves on the agent's ``PATH``.
    """
    return EditorExtras(
        resources=(
            HostResource(broker.socket_path, HostResourceAccess.READ_WRITE, "host command broker"),
            HostResource(launcher.parent, HostResourceAccess.READ_ONLY, "host command client"),
            HostResource(
                launcher,
                HostResourceAccess.READ_ONLY,
                "host command client on PATH",
                agent_path=f"{CLIENT_BIN_DIR}/{launcher.name}",
            ),
        ),
        env={
            COMMAND_BROKER_SOCKET_ENV: str(broker.socket_path),
            COMMAND_BROKER_TOKEN_ENV: broker.token,
            **(env or {}),
        },
        same_path_workspace=True,
        attach_accelerator=False,
    )
