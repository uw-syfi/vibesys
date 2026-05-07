"""agentshim command executor for vibeserve Modal sandboxes.

Companion to :mod:`vibeserve_agent.agents.docker_executor`.  Drives a
running ``modal.Sandbox`` (wrapped by :class:`ModalSandbox`) via the
Modal Python SDK rather than ``modal shell -c``: the latter does not
reliably forward stdin/EOF to the remote process, which is fatal for
CLI agents that push a prompt and expect the child to exit on close.

The sandbox's CLI binary (codex, claude, gemini, opencode) must be
installed inside the sandbox via ``ModalSandbox.extra_init_commands``
before the executor is used; ``find_binary`` therefore returns the
bare binary name and ``check_binary`` is a no-op.
"""

from __future__ import annotations

import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentshim.executor import CommandRequest, CommandResult, CommandStreamSink


@dataclass
class ModalCommandHandle:
    """Command handle for a running modal ``ContainerProcess``."""

    container_process: Any

    def terminate(self) -> None:
        term = getattr(self.container_process, "terminate", None)
        if term is None:
            return
        try:
            term()
        except Exception:
            pass

    def kill(self) -> None:
        # Modal exposes no separate kill signal; reuse terminate.
        self.terminate()


class ModalCommandExecutor:
    """Run agentshim CLI commands inside an already-running Modal sandbox.

    The constructor takes the wrapping :class:`ModalSandbox` (not the raw
    ``modal.Sandbox``) so the executor can re-read the underlying
    ``_sandbox`` on every :meth:`run` call.  This means a fallback restart
    inside :class:`ModalSandbox._restart_sandbox` is picked up
    automatically — the next CLI invocation targets the new container
    without the caller needing to swap executors.
    """

    def __init__(self, modal_sandbox: Any, workdir: str = "/workspace") -> None:
        self._modal_sandbox = modal_sandbox
        self._workdir = workdir

    def find_binary(self, binary_name: str, env: dict[str, str]) -> str:  # noqa: ARG002
        return binary_name

    def check_binary(
        self,
        binary_path: str,  # noqa: ARG002
        env: dict[str, str],  # noqa: ARG002
        *,
        timeout: int,  # noqa: ARG002
    ) -> None:
        return None

    def run(
        self,
        request: CommandRequest,
        sink: CommandStreamSink,
    ) -> CommandResult:
        modal_sb = getattr(self._modal_sandbox, "_sandbox", None)
        if modal_sb is None:
            raise RuntimeError(
                "Modal sandbox not started — call ModalSandbox.start() first"
            )

        cwd = request.cwd or self._workdir

        # Modal bakes env at sandbox-create time; runtime env injection on
        # ``Sandbox.exec`` isn't supported.  Layer any per-invoke overrides
        # in via ``env VAR=val ...`` prefix when present.
        argv = list(request.argv)
        if request.env:
            # Don't leak the host's interactive shell env into the sandbox —
            # ``request.env`` is populated by agentshim's
            # ``get_interactive_env()`` at agent construction time and
            # contains host-specific vars like ``HOME``, ``LOGNAME``,
            # ``MAMBA_*``, ``TMUX``, etc.  Those override the
            # sandbox's defaults and break tools that key off ``HOME`` (e.g.
            # codex, which looks for ``$HOME/.codex/auth.json``).
            #
            # The Modal sandbox already has its env baked in at create-time
            # (the writable codex auth volume mounts at ``/root/.codex``,
            # which is reachable as ``$HOME/.codex`` only when ``HOME=/root``
            # — the in-sandbox default).  Forward only an allowlist of vars
            # that callers might legitimately want to override per invoke.
            _SAFE_ENV_KEYS = {
                "CUDA_VISIBLE_DEVICES",
                "HF_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
                "PYTHONUNBUFFERED",
            }
            forwarded = {
                k: v for k, v in request.env.items() if k in _SAFE_ENV_KEYS
            }
            if forwarded:
                env_prefix = ["env"] + [
                    f"{k}={shlex.quote(v)}" for k, v in forwarded.items()
                ]
                argv = env_prefix + argv

        container_proc = modal_sb.exec(
            *argv,
            workdir=cwd,
            text=True,
            bufsize=1,
        )

        sink.started(ModalCommandHandle(container_proc))

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _drain(
            stream: Any,
            cb: Callable[[str], None],
            collected: list[str],
        ) -> None:
            if stream is None:
                return
            try:
                for line in stream:
                    if not line:
                        break
                    collected.append(line)
                    cb(line)
            except Exception:
                pass

        stdout_thread = threading.Thread(
            target=_drain,
            args=(container_proc.stdout, sink.stdout, stdout_lines),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain,
            args=(container_proc.stderr, sink.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        if request.stdin and getattr(container_proc, "stdin", None) is not None:
            try:
                container_proc.stdin.write(request.stdin)
                # Modal's StreamWriter exposes write_eof rather than
                # close-as-EOF.  ``drain`` flushes the write buffer.
                if hasattr(container_proc.stdin, "write_eof"):
                    container_proc.stdin.write_eof()
                if hasattr(container_proc.stdin, "drain"):
                    try:
                        container_proc.stdin.drain()
                    except Exception:
                        pass
            except Exception:
                pass

        # ``ContainerProcess.wait`` does not currently accept a timeout.
        # ``request.timeout`` is enforced by the sandbox-level timeout
        # baked into ``Sandbox.exec`` at create time.
        returncode = container_proc.wait()

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

        return CommandResult(
            returncode=returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )
