"""``vibesys control <run> <action>``: control a headless run from outside it.

A run started without the server has no socket to send controls on. This command writes one
request into the run's control inbox (``vs_project``'s ``ControlInbox``), and the run applies
it at its next control poll, or after it resumes if it is not running now. The request is the
same ``ControlRequest`` the server's ``command.control`` carries, validated here before it is
written, so an invalid request is refused at once with the offending option named.

Each request gets an id that orders it after every earlier one from this host: milliseconds
since the epoch, then a random suffix: the request's ``control_id``, which also names its file
in the inbox. ``--control-id`` names it instead, so a caller that
retries after an uncertain write posts the same request again rather than a second one.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.api import CONTROL_REQUEST, StopMode
from vs_project.api import Project, ProjectError

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
    parser.add_argument(
        "--control-id", help="the request's id; reusing one posts the same request again"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    steer = actions.add_parser("steer", help="give the run's next agent turn guidance")
    steer.add_argument("text", help="the guidance")
    to = steer.add_mutually_exclusive_group()
    for kind in ("role", "attempt", "item"):
        to.add_argument(
            f"--{kind}",
            metavar="ID",
            help=f"give it to the next turn of this {kind} only (default: the whole run)",
        )
    steer.add_argument(
        "--standing",
        action="store_true",
        help="give it to every turn of the target from now on, until withdrawn",
    )
    withdraw = actions.add_parser(
        "withdraw", help="end standing guidance sent earlier as a standing steer"
    )
    withdraw.add_argument("withdraws", metavar="CONTROL_ID", help="the standing steer's id")
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


def _payload(arguments: argparse.Namespace, control_id: str) -> dict[str, object]:
    common = {"control_id": control_id, "action": arguments.action}
    match arguments.action:
        case "steer":
            kinds = ("role", "attempt", "item")
            named = [(kind, getattr(arguments, kind)) for kind in kinds]
            target = next(({"kind": k, "id": i} for k, i in named if i is not None), None)
            mode = "standing" if arguments.standing else "next_turn"
            targeted = {"target": target} if target else {}
            return {**common, "text": arguments.text, "mode": mode, **targeted}
        case "withdraw":
            return {**common, "withdraws": arguments.withdraws}
        case "resume":
            return {**common, "fallback": arguments.fallback, "when": arguments.when}
        case "stop":
            return {**common, "mode": arguments.mode}
        case _:
            return common


def control_main(
    argv: list[str],
    *,
    cwd: Path,
    new_control_id: Callable[[], str],
    out: TextIO,
    err: TextIO,
) -> int:
    """Validate one control request and post it to the run's inbox; the exit status.

    ``new_control_id`` names a request posted without ``--control-id`` (``time_ordered_id``).
    """
    arguments = _parser().parse_args(argv)
    try:
        request = CONTROL_REQUEST.validate_python(
            _payload(arguments, arguments.control_id or new_control_id())
        )
        project = Project.open(arguments.project or cwd)
        project.state.load_run(arguments.run_id)
        project.state.require_local_run(arguments.run_id)
        project.state.control_inbox(arguments.run_id).post(
            request.control_id, request.model_dump_json().encode()
        )
    except (ValidationError, ProjectError) as error:
        err.write(f"vibesys control: {error}\n")
        return _USAGE_ERROR
    out.write(f"{request.control_id}\n")
    return 0
