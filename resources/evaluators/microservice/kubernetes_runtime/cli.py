"""CLI wrapper that keeps Kubernetes lifecycle ownership foreground-bound."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

EVALUATOR_ROOT = Path(__file__).resolve().parents[1]
if str(EVALUATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALUATOR_ROOT))

from kubernetes_runtime.control import LifecycleControlServer, request_action  # noqa: E402
from kubernetes_runtime.runtime import KubernetesLifecycle, load_config  # noqa: E402

_STOP_COMMAND_PLACEHOLDER = "${KUBERNETES_STOP_COMMAND_JSON}"
_START_COMMAND_PLACEHOLDER = "${KUBERNETES_START_COMMAND_JSON}"
_CLEANUP_COMMAND_PLACEHOLDER = "${KUBERNETES_CLEANUP_COMMAND_JSON}"


def _control_command(socket_path: Path, action: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--control", str(socket_path), action]


def _render_command(
    command: list[str], lifecycle: KubernetesLifecycle, socket_path: Path
) -> list[str]:
    replacements = {
        "${BASE_URL}": lifecycle.base_url,
        _STOP_COMMAND_PLACEHOLDER: json.dumps(_control_command(socket_path, "stop")),
        _START_COMMAND_PLACEHOLDER: json.dumps(_control_command(socket_path, "start")),
        _CLEANUP_COMMAND_PLACEHOLDER: json.dumps(_control_command(socket_path, "cleanup")),
    }
    replacements.update(
        {f"${{ENDPOINT:{name}}}": endpoint for name, endpoint in lifecycle.endpoints.items()}
    )
    rendered = command
    for token, value in replacements.items():
        rendered = [part.replace(token, value) for part in rendered]
    return rendered


def _run_control(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("action", choices=("stop", "start", "cleanup"))
    arguments = parser.parse_args(argv)
    request_action(arguments.control, arguments.action)
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    """Run an evaluator command inside an owned Kubernetes lifecycle."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--control"]:
        return _run_control(argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("an evaluator command is required after --")
    config_path = arguments.config.resolve()
    lifecycle = KubernetesLifecycle(
        load_config(config_path), arguments.candidate_dir, config_dir=config_path.parent
    )
    child: subprocess.Popen[bytes] | None = None
    previous: dict[signal.Signals, Any] = {}

    def terminate(_signal_number: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        raise KeyboardInterrupt

    for handled in (signal.SIGINT, signal.SIGTERM):
        previous[handled] = signal.signal(handled, terminate)
    try:
        lifecycle.start()
        with tempfile.TemporaryDirectory(prefix="vibesys-k8s-control-") as directory:
            socket_path = Path(directory) / "control.sock"
            rendered = _render_command(command, lifecycle, socket_path)
            environment = {**os.environ, "VIBESYS_BASE_URL": lifecycle.base_url}
            for name, endpoint in lifecycle.endpoints.items():
                environment[f"VIBESYS_ENDPOINT_{name.upper().replace('-', '_')}"] = endpoint
            environment["VIBESYS_KUBERNETES_CONTROL_SOCKET"] = str(socket_path)
            actions: dict[str, Callable[[], None]] = {
                "stop": lifecycle.stop,
                "start": lifecycle.start_stopped,
                "cleanup": lifecycle.close,
            }
            with LifecycleControlServer(socket_path, actions):
                child = subprocess.Popen(  # noqa: S603
                    rendered, env=environment, start_new_session=True
                )
                return child.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        lifecycle.close()
        for handled, handler in previous.items():
            signal.signal(handled, handler)


if __name__ == "__main__":
    raise SystemExit(main())
