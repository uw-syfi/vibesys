"""agentshim command executor for existing vibeserve Docker sandboxes."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agentshim.executor import CommandRequest, CommandResult, CommandStreamSink


@dataclass
class DockerCommandHandle:
    """Command handle for a running ``docker exec`` process."""

    process: subprocess.Popen[str]

    def terminate(self) -> None:
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            self.process.terminate()

    def kill(self) -> None:
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            self.process.kill()


class DockerCommandExecutor:
    """Run agentshim CLI commands inside an already-running Docker container."""

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id

    def find_binary(self, binary_name: str, env: dict[str, str]) -> str:
        return binary_name

    def check_binary(
        self,
        binary_path: str,
        env: dict[str, str],
        *,
        timeout: int,
    ) -> None:
        return None

    def run(
        self,
        request: CommandRequest,
        sink: CommandStreamSink,
    ) -> CommandResult:
        docker_cmd = [
            "docker",
            "exec",
            "-i",
            "-w",
            "/workspace",
            self.container_id,
            *request.argv,
        ]
        process = subprocess.Popen(
            docker_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
            start_new_session=True,
        )

        sink.started(DockerCommandHandle(process))

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def read_pipe(pipe: Any, callback: Callable[[str], None], sink: list[str]) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    if not line:
                        break
                    sink.append(line)
                    callback(line)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

        stdout_thread = threading.Thread(
            target=read_pipe,
            args=(process.stdout, sink.stdout, stdout_lines),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_pipe,
            args=(process.stderr, sink.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            if process.stdin:
                process.stdin.write(request.stdin)
                process.stdin.close()
        except BrokenPipeError:
            pass

        watchdog_killed = False
        try:
            if request.timeout is not None:
                process.wait(timeout=request.timeout)
            else:
                watchdog_killed = self._wait_with_docker_exec_watchdog(process, request.argv)
        except subprocess.TimeoutExpired:
            self._kill_process_group(process)
            process.wait()
            raise subprocess.TimeoutExpired(list(request.argv), request.timeout) from None
        finally:
            if process.poll() is None:
                self._kill_process_group(process)
                process.wait()

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)

        returncode = 0 if watchdog_killed else process.returncode
        return CommandResult(
            returncode=returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )

    def _wait_with_docker_exec_watchdog(
        self,
        process: subprocess.Popen[str],
        cmd: Sequence[str],
    ) -> bool:
        child_binary = os.path.basename(cmd[0]) if cmd else ""
        watchdog_killed = False
        while True:
            try:
                process.wait(timeout=5)
                return watchdog_killed
            except subprocess.TimeoutExpired:
                check = subprocess.run(
                    [
                        "docker",
                        "exec",
                        self.container_id,
                        "pgrep",
                        "-f",
                        child_binary,
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if check.returncode != 0:
                    watchdog_killed = True
                    process.kill()
                    process.wait()
                    return watchdog_killed

    def _kill_process_group(self, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
