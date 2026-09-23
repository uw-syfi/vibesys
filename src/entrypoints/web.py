"""Developer and operator helpers for the VibeSys web UI."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from server.runtime import WebInstanceRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

_LIVE_PORT = 8765
_DEV_PORT = 5173
_MAX_PORT = 65_535
_RECORD_WAIT_SECONDS = 10.0
_DEMO_PROJECT = Path("examples/data-structures/repositories/queue-rs")
_DEMO_TASK = "spsc"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None  # noqa: TRY003
    if not 1 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")  # noqa: TRY003
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys web",
        description="Develop, launch, and tunnel the VibeSys web UI.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    dev = commands.add_parser("dev", help="serve the replay UI with Vite")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=_port, default=_DEV_PORT)

    live = commands.add_parser("live", help="launch a live gateway and print its browser URL")
    live.add_argument("--project", type=Path, default=None)
    live.add_argument("--task", default=None)
    live.add_argument("--port", type=_port, default=_LIVE_PORT)
    live.add_argument("--instance", type=Path, default=None)
    live.add_argument("--ssh-target", default=None, metavar="USER@HOST")
    live.add_argument("--browser-origin", action="append", default=[])
    live.add_argument("--demo", action="store_true")
    live.add_argument("--no-build", action="store_true")
    live.add_argument("--open", action="store_true", help="ask the host to open a browser")
    live.add_argument(
        "run_args",
        nargs=argparse.REMAINDER,
        help="additional run arguments after --",
    )

    tunnel = commands.add_parser("tunnel", help="forward a remote gateway to this laptop")
    tunnel.add_argument("--host", required=True, metavar="USER@HOST")
    tunnel.add_argument("--url", required=True, help="capability URL printed by `vibesys web live`")
    tunnel.add_argument("--local-port", type=_port, default=None)
    tunnel.add_argument("--browser-origin", default="http://127.0.0.1:5173")

    stop = commands.add_parser("stop", help="stop a detached gateway")
    stop.add_argument("--instance", type=Path, required=True)

    status = commands.add_parser("status", help="show a detached gateway status")
    status.add_argument("--instance", type=Path, required=True)
    return parser


def _pnpm() -> str:
    executable = shutil.which("pnpm")
    if executable is None:
        raise SystemExit("vibesys web: pnpm is required; install pnpm and try again")  # noqa: TRY003
    return executable


def _ssh() -> str:
    executable = shutil.which("ssh")
    if executable is None:
        raise SystemExit("vibesys web: ssh is required for tunnel mode")  # noqa: TRY003
    return executable


def _run_dev(args: argparse.Namespace, root: Path) -> int:
    print(f"VibeSys replay UI: http://{args.host}:{args.port}", flush=True)  # noqa: T201
    return subprocess.run(  # noqa: S603
        [_pnpm(), "dev", "--host", args.host, "--port", str(args.port)],
        cwd=root / "clients" / "web",
        check=False,
    ).returncode


def _demo_project(source: Path) -> Path:
    source = source.resolve()
    if not source.is_dir():
        raise SystemExit(f"vibesys web: demo project does not exist: {source}")  # noqa: TRY003
    target_root = Path(tempfile.mkdtemp(prefix="vibesys-web-demo-"))
    target = target_root / source.name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
    return target


def _live_command(  # noqa: PLR0913
    *,
    project: Path,
    task: str | None,
    port: int,
    instance: Path,
    demo: bool,
    run_args: Sequence[str],
    browser_origins: Sequence[str],
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "entrypoints.server",
        "--web",
        "--detach",
        "--web-port",
        str(port),
        "--web-instance",
        str(instance),
        "--project",
        str(project),
    ]
    if task is not None:
        command.extend(("--task", task))
    for origin in browser_origins:
        command.extend(("--web-origin", origin))
    if demo:
        command.extend(
            (
                "--stub-agent",
                "--local",
                "--run-environment",
                "local",
                "--outer-loop",
                "agent",
                "--max-rounds",
                "1",
            )
        )
    command.extend(run_args[1:] if run_args and run_args[0] == "--" else run_args)
    return command


def _wait_for_record(path: Path) -> WebInstanceRecord:
    deadline = time.monotonic() + _RECORD_WAIT_SECONDS
    while time.monotonic() < deadline:
        record = WebInstanceRecord.discover(path, cleanup_stale=False)
        if record is not None:
            return record
        time.sleep(0.05)
    raise SystemExit(f"vibesys web: gateway did not publish {path}")  # noqa: TRY003


def _run_live(args: argparse.Namespace, root: Path) -> int:
    source_project = (args.project or root / _DEMO_PROJECT).expanduser().resolve()
    project = _demo_project(source_project) if args.demo else source_project
    task = args.task or _DEMO_TASK if args.demo and args.project is None else args.task
    if not project.is_dir():
        raise SystemExit(f"vibesys web: project does not exist: {project}")  # noqa: TRY003
    if not args.demo and args.project is None:
        raise SystemExit("vibesys web live: pass --project or use --demo")  # noqa: TRY003
    instance = (args.instance or project / ".vibesys" / "web-gateway.json").expanduser().resolve()
    if not args.no_build:
        subprocess.run(  # noqa: S603
            [_pnpm(), "build"],
            cwd=root / "clients" / "web",
            check=True,
        )
    environment = os.environ.copy()
    if not args.open:
        environment["BROWSER"] = "true"
    command = _live_command(
        project=project,
        task=task,
        port=args.port,
        instance=instance,
        demo=args.demo,
        run_args=args.run_args,
        browser_origins=args.browser_origin,
    )
    subprocess.run(command, cwd=root, env=environment, check=True)  # noqa: S603
    record = _wait_for_record(instance)
    print(f"VibeSys web UI ready: {record.url}", flush=True)  # noqa: T201
    print(f"Instance record: {instance}", flush=True)  # noqa: T201
    if args.ssh_target is not None:
        print(  # noqa: T201
            f"SSH tunnel: ssh -N -L {args.port}:127.0.0.1:{args.port} {args.ssh_target}"
        )
        print(  # noqa: T201
            f"Open locally: http://127.0.0.1:{args.port}/?token={record.token}"
        )
    if args.browser_origin:
        print(f"Browser harness URL: {_browser_url(args.browser_origin[0], record.url)}")  # noqa: T201
    return 0


def _local_url(remote_url: str, local_port: int) -> tuple[str, int]:
    parsed = urlsplit(remote_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise SystemExit(  # noqa: TRY003
            "vibesys web tunnel: URL must be an http://127.0.0.1 capability URL"
        )
    token = parse_qs(parsed.query).get("token", [""])[0]
    if not token:
        raise SystemExit("vibesys web tunnel: URL is missing its capability token")  # noqa: TRY003
    local = urlunsplit(
        ("http", f"127.0.0.1:{local_port}", parsed.path, urlencode({"token": token}), "")
    )
    return local, parsed.port


def _browser_url(origin: str, gateway_url: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise SystemExit(  # noqa: TRY003
            "vibesys web: browser origin must be an http:// or https:// origin without a path"
        )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode({"gateway": gateway_url}), "")
    )


def _run_tunnel(args: argparse.Namespace) -> int:
    parsed = urlsplit(args.url)
    remote_port = parsed.port
    if remote_port is None:
        raise SystemExit("vibesys web tunnel: URL is missing its port")  # noqa: TRY003
    local_port = args.local_port or remote_port
    if local_port != remote_port:
        raise SystemExit(  # noqa: TRY003
            "vibesys web tunnel: local and remote ports must match for strict WebSocket origins"
        )
    local_url, _ = _local_url(args.url, local_port)
    print(f"Open locally: {local_url}", flush=True)  # noqa: T201
    print(f"Browser harness URL: {_browser_url(args.browser_origin, args.url)}", flush=True)  # noqa: T201
    return subprocess.run(  # noqa: S603
        [_ssh(), "-N", "-L", f"{local_port}:127.0.0.1:{remote_port}", args.host],
        check=False,
    ).returncode


def _run_status(args: argparse.Namespace) -> int:
    record = WebInstanceRecord.discover(args.instance, cleanup_stale=False)
    if record is None:
        print("No VibeSys web gateway is running.", flush=True)  # noqa: T201
        return 1
    print(f"VibeSys web UI: {record.url}", flush=True)  # noqa: T201
    print(f"PID: {record.pid}", flush=True)  # noqa: T201
    return 0


def _run_stop(args: argparse.Namespace) -> int:
    record = WebInstanceRecord.discover(args.instance, cleanup_stale=False)
    if record is None:
        print("No VibeSys web gateway is running.", flush=True)  # noqa: T201
        return 0
    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + _RECORD_WAIT_SECONDS
    while time.monotonic() < deadline and args.instance.exists():
        time.sleep(0.05)
    print(f"Stopped VibeSys web gateway {record.pid}.", flush=True)  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run a web UI development or operator helper."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = _repository_root()
    if args.command == "dev":
        return _run_dev(args, root)
    if args.command == "live":
        return _run_live(args, root)
    if args.command == "tunnel":
        return _run_tunnel(args)
    if args.command == "status":
        return _run_status(args)
    if args.command == "stop":
        return _run_stop(args)
    raise AssertionError(f"Unhandled web command: {args.command}")  # noqa: TRY003


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
