"""``vibesys control <run> <action>``: control a headless run from outside it.

A run started without the server has no socket to send controls on. This command writes one
request into the run's control inbox (``vs_project``'s ``ControlInbox``), and the run applies
it at its next control poll, or after it resumes if it is not running now. The request is the
same ``ControlRequest`` the server's ``command.control`` carries, validated here before it is
written, so an invalid request is refused at once with the offending option named.

Each request gets an id that orders it after every earlier one from this host: milliseconds
since the epoch, then a random suffix. ``--request-id`` names it instead, so a caller that
retries after an uncertain write posts the same request again rather than a second one.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.api import CONTROL_REQUEST, StopMode
from vs_project.api import Project, ProjectError, validate_request_id

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TextIO

__all__ = ["control_main", "time_ordered_id"]

_USAGE_ERROR = 2


def time_ordered_id(now: float, token: str) -> str:
    """A request id that sorts after every earlier one: epoch milliseconds, then ``token``."""
    return f"{round(now * 1000):013d}-{token}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys control",
        description="Send a control to a run through its control inbox.",
    )
    parser.add_argument("run_id", help="the run to control")
    parser.add_argument(
        "--project", help="the run's project directory (default: the current directory)"
    )
    parser.add_argument("--request-id", help="the request's id; reusing one posts it again")
    actions = parser.add_subparsers(dest="action", required=True)
    steer = actions.add_parser("steer", help="give the run's next agent turn guidance")
    steer.add_argument("text", help="the guidance")
    actions.add_parser("pause", help="hold the run at its next safe boundary")
    resume = actions.add_parser("resume", help="continue a paused run")
    how = resume.add_mutually_exclusive_group()
    how.add_argument(
        "--fallback",
        action="store_true",
        help="switch a run paused on a provider capacity limit to its fallback provider",
    )
    how.add_argument(
        "--when",
        choices=("now", "capacity_returns"),
        default="now",
        help="resume now, or once provider capacity returns",
    )
    stop = actions.add_parser("stop", help="end the run")
    stop.add_argument(
        "--mode",
        choices=[mode.value for mode in StopMode],
        default=StopMode.DRAIN.value,
        help="drain the work in flight, or cancel it",
    )
    return parser


def _payload(arguments: argparse.Namespace) -> dict[str, object]:
    match arguments.action:
        case "steer":
            return {"action": "steer", "text": arguments.text}
        case "resume":
            return {"action": "resume", "fallback": arguments.fallback, "when": arguments.when}
        case "stop":
            return {"action": "stop", "mode": arguments.mode}
        case action:
            return {"action": action}


def control_main(
    argv: list[str],
    *,
    cwd: Path,
    new_request_id: Callable[[], str],
    out: TextIO,
    err: TextIO,
) -> int:
    """Validate one control request and post it to the run's inbox; the exit status.

    ``new_request_id`` names a request posted without ``--request-id`` (``time_ordered_id``).
    """
    arguments = _parser().parse_args(argv)
    try:
        request = CONTROL_REQUEST.validate_python(_payload(arguments))
        request_id = validate_request_id(arguments.request_id or new_request_id())
        project = Project.open(arguments.project or cwd)
        project.state.load_run(arguments.run_id)
        project.state.control_inbox(arguments.run_id).post(
            request_id, request.model_dump_json().encode()
        )
    except (ValidationError, ProjectError) as error:
        err.write(f"vibesys control: {error}\n")
        return _USAGE_ERROR
    out.write(f"{request_id}\n")
    return 0
