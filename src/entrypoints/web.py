"""Developer and operator helpers for the VibeSys web UI."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from entrypoints.server import (
    GATEWAY_STOP_TIMEOUT_SECONDS,
    GatewayPortStopOutcome,
    GatewayPortStopResult,
    GatewayStopOutcome,
    GatewayStopResult,
    stop_detached_gateway,
    stop_web_gateway_on_port,
)
from server.runtime import (
    CAPABILITY_ROTATION_HEADER,
    WebInstanceHold,
    WebInstanceRecord,
    WebPortInspector,
    WebPortObservation,
    WebPortState,
)
from vs_project.api import Project
from vs_sim.api import InheritedStdioLauncher, OsThreads, run_foreground

if TYPE_CHECKING:
    from collections.abc import Sequence

    from entrypoints.server import WebGatewayPortEffects, WebGatewayStopEffects
    from vs_sim.api import ForegroundLauncher, Threads

_LIVE_PORT = 8765
_DEV_PORT = 5173
_MAX_PORT = 65_535
_RECORD_WAIT_SECONDS = 10.0
_DEMO_LOG = Path("clients/tui/dev/fixtures/framework-events.jsonl")
_DEMO_RUNTIME = Path("clients/web/.vibesys-demo")


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None  # noqa: TRY003  # lint-waiver: LW-101070 [TRY003]; report malformed web helper port input
    if not 1 <= port <= _MAX_PORT:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")  # noqa: TRY003  # lint-waiver: LW-101071 [TRY003]; enforce the browser helper's valid TCP port range
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
    source = live.add_mutually_exclusive_group()
    source.add_argument("--project", type=Path, default=None)
    source.add_argument("--demo", action="store_true")
    live.add_argument("--task", default=None)
    live.add_argument("--port", type=_port, default=_LIVE_PORT)
    live.add_argument("--instance", type=Path, default=None)
    live.add_argument("--ssh-target", default=None, metavar="USER@HOST")
    live.add_argument("--browser-origin", action="append", default=[])
    live.add_argument(
        "--no-build",
        action="store_true",
        help=(
            "reuse the last verified clients/web build; the gateway rejects it if the "
            "workspace sources have changed"
        ),
    )
    live.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="ask the host to open a browser",
    )
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

    stop = commands.add_parser(
        "stop", help="stop a detached gateway and wait for it to release its instance files"
    )
    stop_target = stop.add_mutually_exclusive_group(required=True)
    stop_target.add_argument("--instance", type=Path)
    stop_target.add_argument("--port", type=_port)

    status = commands.add_parser("status", help="show a detached gateway status")
    status_target = status.add_mutually_exclusive_group(required=True)
    status_target.add_argument("--instance", type=Path)
    status_target.add_argument("--port", type=_port)

    rotate = commands.add_parser("rotate", help="replace a gateway's browser launch capability")
    rotate.add_argument("--instance", type=Path, required=True)
    return parser


def _pnpm() -> str:
    executable = shutil.which("pnpm")
    if executable is None:
        raise SystemExit("vibesys web: pnpm is required; install pnpm and try again")  # noqa: TRY003  # lint-waiver: LW-101072 [TRY003]; provide an actionable dependency error for replay and build modes
    return executable


def _ssh() -> str:
    executable = shutil.which("ssh")
    if executable is None:
        raise SystemExit("vibesys web: ssh is required for tunnel mode")  # noqa: TRY003  # lint-waiver: LW-101073 [TRY003]; provide an actionable dependency error for tunnel mode
    return executable


def _run_foreground(
    children: ForegroundLauncher | None,
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run *argv* on the terminal until it exits and return its status."""
    launcher = InheritedStdioLauncher() if children is None else children
    return asyncio.run(run_foreground(launcher, argv, env=env, cwd=cwd))


def _run_dev(
    args: argparse.Namespace, root: Path, children: ForegroundLauncher | None = None
) -> int:
    print(f"VibeSys replay UI: http://{args.host}:{args.port}", flush=True)  # noqa: T201  # lint-waiver: LW-101074 [T201]; expose the browser URL to the developer
    return _run_foreground(
        children,
        [_pnpm(), "dev", "--host", args.host, "--port", str(args.port)],
        cwd=root / "clients" / "web",
    )


def _live_command(  # noqa: PLR0913  # lint-waiver: LW-101077 [PLR0913]; keep independent live-launch options explicit at this composition boundary
    *,
    project: Path | None,
    replay_log: Path | None,
    task: str | None,
    port: int,
    instance: Path,
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
    ]
    if replay_log is not None:
        if project is not None:
            raise AssertionError
        command.extend(("--web-reopen", str(replay_log)))
    elif project is not None:
        command.extend(("--project", str(project)))
        if task is not None:
            command.extend(("--task", task))
    else:
        raise AssertionError
    for origin in browser_origins:
        command.extend(("--web-origin", origin))
    if replay_log is None:
        command.extend(run_args[1:] if run_args and run_args[0] == "--" else run_args)
    return command


def _wait_for_record(path: Path, threads: Threads | None = None) -> WebInstanceRecord:
    """Poll for the gateway's record on *threads*' clock (the real one by default)."""
    clock = OsThreads() if threads is None else threads
    deadline = clock.now() + _RECORD_WAIT_SECONDS
    while clock.now() < deadline:
        record = WebInstanceRecord.discover(path, cleanup_stale=False)
        if record is not None:
            return record
        clock.sleep(0.05)
    raise SystemExit(f"vibesys web: gateway did not publish {path}")  # noqa: TRY003  # lint-waiver: LW-101078 [TRY003]; report the bounded detached-startup timeout


class WebLiveEffects(Protocol):
    """The build, process, and discovery effects of a live web launch."""

    def build(self, root: Path) -> None:
        """Build the browser bundle, raising on failure."""
        ...

    def launch(self, command: list[str], root: Path, environment: dict[str, str]) -> int:
        """Run the server launcher and return its process status."""
        ...

    def wait_for_record(self, instance: Path) -> WebInstanceRecord:
        """Return the record published by a successful launch."""
        ...


class _DefaultWebLiveEffects:
    """Builds and launches on the terminal, through *children* (the real system by default)."""

    def __init__(self, children: ForegroundLauncher | None = None) -> None:
        self._children = children

    def build(self, root: Path) -> None:
        status = _run_foreground(self._children, [_pnpm(), "build"], cwd=root / "clients" / "web")
        if status != 0:
            raise SystemExit(f"vibesys web: the web bundle build failed with status {status}")  # noqa: TRY003  # lint-waiver: LW-101081 [TRY003]; report a failed bundle build as the command's failure

    def launch(self, command: list[str], root: Path, environment: dict[str, str]) -> int:
        return _run_foreground(self._children, command, cwd=root, env=environment)

    def wait_for_record(self, instance: Path) -> WebInstanceRecord:
        return _wait_for_record(instance)


_LIVE_EFFECTS = _DefaultWebLiveEffects()


def _run_live(
    args: argparse.Namespace,
    root: Path,
    effects: WebLiveEffects = _LIVE_EFFECTS,
) -> int:
    _validate_live_arguments(args)
    if args.demo:
        replay_source = (root / _DEMO_LOG).resolve()
        if not replay_source.is_file():
            raise SystemExit(f"vibesys web: demo event log does not exist: {replay_source}")  # noqa: TRY003  # lint-waiver: LW-101104 [TRY003]; report an incomplete source checkout before gateway startup
        demo_dir = (root / _DEMO_RUNTIME).resolve()
        demo_dir.mkdir(parents=True, exist_ok=True)
        replay_log = demo_dir / "run-events.jsonl"
        shutil.copy2(replay_source, replay_log)
        project = None
        default_instance = demo_dir / "web-gateway.json"
    else:
        replay_log = None
        project = args.project.expanduser().resolve()
        if not project.is_dir():
            raise SystemExit(f"vibesys web: project does not exist: {project}")  # noqa: TRY003  # lint-waiver: LW-101079 [TRY003]; report an invalid live project before launching the server
        default_instance = Project.open(project).configuration_path() / "web-gateway.json"
    instance = (args.instance or default_instance).expanduser().resolve()
    if not args.no_build:
        effects.build(root)
    environment = os.environ.copy()
    if not args.open:
        environment["BROWSER"] = "true"
    command = _live_command(
        project=project,
        replay_log=replay_log,
        task=args.task,
        port=args.port,
        instance=instance,
        run_args=args.run_args,
        browser_origins=args.browser_origin,
    )
    launch_status = effects.launch(command, root, environment)
    if launch_status != 0:
        return launch_status
    record = effects.wait_for_record(instance)
    print(f"VibeSys web UI ready: {record.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101083 [T201]; expose the capability URL to the operator
    if record.web_build_id is not None:
        print(f"Web build: {record.web_build_id}", flush=True)  # noqa: T201  # lint-waiver: LW-106101 [T201]; expose the exact browser artifact served by the detached gateway
    print(f"Instance record: {instance}", flush=True)  # noqa: T201  # lint-waiver: LW-101084 [T201]; expose the lifecycle record path to the operator
    if args.ssh_target is not None:
        print(  # noqa: T201  # lint-waiver: LW-101085 [T201]; expose the exact SSH tunnel command to the operator
            f"SSH tunnel: ssh -N -L {args.port}:127.0.0.1:{args.port} {args.ssh_target}"
        )
        print(  # noqa: T201  # lint-waiver: LW-101086 [T201]; expose the local capability URL to the operator
            f"Open locally: http://127.0.0.1:{args.port}/?token={record.token}"
        )
    if args.browser_origin:
        print(f"Browser harness URL: {_browser_url(args.browser_origin[0], record.url)}")  # noqa: T201  # lint-waiver: LW-101087 [T201]; expose the local browser harness URL to the operator
    return 0


def _validate_live_arguments(args: argparse.Namespace) -> None:
    """Reject combinations that cannot identify one live source."""
    if args.demo and (args.task is not None or args.run_args):
        raise SystemExit("vibesys web live: --demo does not accept run arguments")  # noqa: TRY003  # lint-waiver: LW-101103 [TRY003]; keep the deterministic replay demo separate from operator-owned runs
    if not args.demo and args.project is None:
        raise SystemExit("vibesys web live: pass --project or use --demo")  # noqa: TRY003  # lint-waiver: LW-101080 [TRY003]; require an explicit project for non-demo live mode


def _local_url(remote_url: str, local_port: int) -> tuple[str, int]:
    parsed = urlsplit(remote_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise SystemExit(  # noqa: TRY003  # lint-waiver: LW-101088 [TRY003]; reject capability URLs outside the loopback gateway contract
            "vibesys web tunnel: URL must be an http://127.0.0.1 capability URL"
        )
    token = parse_qs(parsed.query).get("token", [""])[0]
    if not token:
        raise SystemExit("vibesys web tunnel: URL is missing its capability token")  # noqa: TRY003  # lint-waiver: LW-101089 [TRY003]; require the capability token before forwarding a gateway
    local = urlunsplit(
        ("http", f"127.0.0.1:{local_port}", parsed.path, urlencode({"token": token}), "")
    )
    return local, parsed.port


def _browser_url(origin: str, gateway_url: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise SystemExit(  # noqa: TRY003  # lint-waiver: LW-101090 [TRY003]; reject a browser tunnel whose ports would violate exact Origin matching
            "vibesys web: browser origin must be an http:// or https:// origin without a path"
        )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode({"gateway": gateway_url}), "")
    )


def _run_tunnel(args: argparse.Namespace, children: ForegroundLauncher | None = None) -> int:
    parsed = urlsplit(args.url)
    remote_port = parsed.port
    if remote_port is None:
        raise SystemExit("vibesys web tunnel: URL is missing its port")  # noqa: TRY003  # lint-waiver: LW-101091 [TRY003]; require a port for the SSH forward
    local_port = args.local_port or remote_port
    if local_port != remote_port:
        raise SystemExit(  # noqa: TRY003  # lint-waiver: LW-101092 [TRY003]; require identical local and remote ports for exact Origin matching
            "vibesys web tunnel: local and remote ports must match for strict WebSocket origins"
        )
    local_url, _ = _local_url(args.url, local_port)
    print(f"Open locally: {local_url}", flush=True)  # noqa: T201  # lint-waiver: LW-101093 [T201]; expose the forwarded capability URL to the operator
    print(f"Browser harness URL: {_browser_url(args.browser_origin, args.url)}", flush=True)  # noqa: T201  # lint-waiver: LW-101094 [T201]; expose the local browser harness URL to the operator
    return _run_foreground(
        children, [_ssh(), "-N", "-L", f"{local_port}:127.0.0.1:{remote_port}", args.host]
    )


class WebPortStatusInspector(Protocol):
    """The read-only port observation needed by the status command."""

    def inspect(self, port: int) -> WebPortObservation:
        """Return a fail-closed observation of ``port``."""
        ...


def _run_status(
    args: argparse.Namespace,
    inspector: WebPortStatusInspector | None = None,
) -> int:
    port = getattr(args, "port", None)
    if port is not None:
        observation = (inspector or WebPortInspector()).inspect(port)
        print(_port_status_message(observation), flush=True)  # noqa: T201  # lint-waiver: LW-102906 [T201]; port status is the requested operator-facing result
        return 0 if observation.state is WebPortState.VIBESYS_GATEWAY else 1
    record = WebInstanceRecord.discover(args.instance, cleanup_stale=False)
    if record is None:
        print("No VibeSys web gateway is running.", flush=True)  # noqa: T201  # lint-waiver: LW-101096 [T201]; report lifecycle status to the operator
        return 1
    print(f"VibeSys web UI: {record.url}", flush=True)  # noqa: T201  # lint-waiver: LW-101097 [T201]; expose the live capability URL in status output
    print(f"PID: {record.pid}", flush=True)  # noqa: T201  # lint-waiver: LW-101098 [T201]; expose the gateway process identity in status output
    if record.web_build_id is not None:
        print(f"Web build: {record.web_build_id}", flush=True)  # noqa: T201  # lint-waiver: LW-106102 [T201]; identify the browser artifact for lifecycle inspection
    return 0


def _port_status_message(observation: WebPortObservation) -> str:
    authority = f"{observation.host}:{observation.port}"
    gateway = observation.gateway
    if observation.state is WebPortState.FREE:
        return f"No process is listening on {authority}."
    if observation.state is WebPortState.VIBESYS_GATEWAY and gateway is not None:
        return (
            f"VibeSys web gateway on {authority}. PID: {gateway.pid}. "
            f"Instance: {gateway.instance_path}."
        )
    pids = ", ".join(str(pid) for pid in observation.holder_pids)
    owners = f" Same-user listener PIDs: {pids}." if pids else ""
    if observation.state is WebPortState.OTHER:
        return f"A non-VibeSys process is listening on {authority}.{owners}"
    return f"Cannot safely identify the listener on {authority}.{owners}"


def _run_rotate(args: argparse.Namespace) -> int:
    record = WebInstanceRecord.discover(args.instance, cleanup_stale=False)
    if record is None:
        print("No VibeSys web gateway is running.", flush=True)  # noqa: T201  # lint-waiver: LW-936001 [T201]; this command's stdout is its user-facing result; routing it through logging would make normal lifecycle status depend on log configuration.
        return 1
    endpoint = urlsplit(record.capability_rotation_url)
    connection = HTTPConnection("127.0.0.1", record.port, timeout=2)
    try:
        connection.request(
            "GET",
            urlunsplit(("", "", endpoint.path, endpoint.query, "")),
            headers={CAPABILITY_ROTATION_HEADER: "1"},
        )
        response = connection.getresponse()
        response.read()
    except OSError as error:
        print(f"Unable to rotate VibeSys web capability: {error}", flush=True)  # noqa: T201  # lint-waiver: LW-936002 [T201]; this command's stdout is its user-facing result; logging would make an actionable transport failure depend on log configuration.
        return 1
    finally:
        connection.close()
    if response.status != HTTPStatus.OK:
        print(f"Unable to rotate VibeSys web capability: HTTP {response.status}", flush=True)  # noqa: T201  # lint-waiver: LW-936003 [T201]; this command's stdout is its user-facing result; logging would make an actionable HTTP rejection depend on log configuration.
        return 1
    replacement = WebInstanceRecord.discover(args.instance, cleanup_stale=False)
    if replacement is None or replacement.token == record.token:
        print("Gateway did not publish a replacement capability.", flush=True)  # noqa: T201  # lint-waiver: LW-936004 [T201]; this command's stdout is its user-facing result; logging would hide an incomplete rotation behind log configuration.
        return 1
    print(f"VibeSys web UI: {replacement.url}", flush=True)  # noqa: T201  # lint-waiver: LW-936005 [T201]; the replacement capability is the command's requested result; logging could suppress it or add prefixes that corrupt copying.
    return 0


def _stop_message(instance: Path, result: GatewayStopResult) -> str:
    if result.outcome is GatewayStopOutcome.NOT_RUNNING:
        return "No VibeSys web gateway is running."
    if result.outcome is GatewayStopOutcome.STILL_HOLDING:
        return _still_in_use_message(instance, result)
    if result.pid is None:
        return f"Waited for a VibeSys web gateway to finish releasing {instance.parent}."
    return f"Stopped VibeSys web gateway {result.pid}."


def _still_in_use_message(instance: Path, result: GatewayStopResult) -> str:
    """Say who is still using the directory and what the operator can do about it.

    The instance record is not a reliable source for that: a descendant that
    inherited the startup log keeps the directory in use with no record naming
    it, and a record left by a killed gateway can name a process that has
    nothing to do with this directory. So the holders come from the
    observation, and the escalation names them rather than pointing at `status`,
    which reads the record and therefore reports "not running" in exactly this
    state.
    """
    directory = instance.parent
    delivery = (
        f"SIGTERM went to gateway {result.pid} {GATEWAY_STOP_TIMEOUT_SECONDS:.0f} seconds ago."
        if result.pid is not None
        else "Nothing was signalled: no instance record named a process that has these files open."
    )
    keep = f"Do not reuse or remove {directory}."
    if result.hold.log_locked is None:
        log_path = WebInstanceHold.log_path(instance)
        return (
            f"Cannot establish that {directory} is free: the lock state of {log_path} "
            f"could not be read. {delivery} {keep}"
        )
    if result.hold.holders:
        pids = ", ".join(str(pid) for pid in result.hold.holders)
        return (
            f"{directory} is still in use. Processes with files open there: {pids}. "
            f"{delivery} {keep} End those processes first (`kill -9 {pids}`)."
        )
    return (
        f"{directory} is still in use: {WebInstanceHold.log_path(instance)} is locked by "
        f"a process this host cannot identify, which means it belongs to another user. "
        f"{delivery} {keep}"
    )


def _run_stop(
    args: argparse.Namespace,
    effects: WebGatewayStopEffects | WebGatewayPortEffects | None = None,
) -> int:
    port = getattr(args, "port", None)
    if port is not None:
        result = stop_web_gateway_on_port(
            port,
            cast("WebGatewayPortEffects | None", effects),
        )
        print(_port_stop_message(result), flush=True)  # noqa: T201  # lint-waiver: LW-102907 [T201]; report the safe port-keyed stop outcome to the operator
        return (
            0
            if result.outcome
            in {GatewayPortStopOutcome.NOT_RUNNING, GatewayPortStopOutcome.STOPPED}
            else 1
        )
    result = stop_detached_gateway(
        args.instance,
        cast("WebGatewayStopEffects | None", effects),
    )
    print(_stop_message(args.instance, result), flush=True)  # noqa: T201  # lint-waiver: LW-101099 [T201]; report the gateway lifecycle outcome to the operator
    return 1 if result.outcome is GatewayStopOutcome.STILL_HOLDING else 0


def _port_stop_message(result: GatewayPortStopResult) -> str:
    observation = result.observation
    authority = f"{observation.host}:{observation.port}"
    if result.outcome is GatewayPortStopOutcome.NOT_RUNNING:
        return f"No process is listening on {authority}."
    if result.outcome is GatewayPortStopOutcome.STOPPED:
        if result.pid is None:
            return f"The listener on {authority} exited before it could be signalled."
        return f"Stopped VibeSys web gateway {result.pid} on {authority}."
    if result.outcome is GatewayPortStopOutcome.STILL_HOLDING:
        return (
            f"VibeSys web gateway {result.pid} is still listening on {authority} after "
            f"{GATEWAY_STOP_TIMEOUT_SECONDS:.0f} seconds."
        )
    if result.pid is not None:
        return (
            f"Gateway {result.pid} stopped or changed identity, but {authority} is still held. "
            "The replacement was not signalled."
        )
    return f"Refused to signal the listener on {authority}. {_port_status_message(observation)}"


def main(argv: list[str] | None = None, children: ForegroundLauncher | None = None) -> int:
    """Run a web UI development or operator helper; *children* starts its terminal processes."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = _repository_root()
    if args.command == "dev":
        return _run_dev(args, root, children)
    if args.command == "live":
        return _run_live(args, root, _DefaultWebLiveEffects(children))
    if args.command == "tunnel":
        return _run_tunnel(args, children)
    if args.command == "status":
        return _run_status(args)
    if args.command == "rotate":
        return _run_rotate(args)
    if args.command == "stop":
        return _run_stop(args)
    raise AssertionError(f"Unhandled web command: {args.command}")  # noqa: TRY003  # lint-waiver: LW-101101 [TRY003]; guard parser dispatch exhaustiveness


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
