"""``vibesys control <run> <action>``: control a headless run from outside it.

A run started without the server has no socket to send controls on. This command writes one
request into the run's control inbox (``vs_project``'s ``ControlInbox``), and the run applies
it at its next control poll, or after it resumes if it is not running now. The request is the
same ``ControlRequest`` the server's ``command.control`` carries, validated here before it is
written, so an invalid request is refused at once with the offending option named.

Each request gets an id: milliseconds since the epoch, then a random suffix. That is the
request's ``control_id``, which also names its file in the inbox. ``--control-id`` names it
instead, so a caller that retries after an uncertain write posts the same request again
rather than a second one. Requests apply in the order they were posted, whatever their ids.

``vibesys control <run> status CONTROL_ID`` says whether a request is pending, applied or
rejected, and why. Whether the run has ended is what its core record says, read anew for
each command. A run that has ended refuses every new request before anything is written and
exits with status 3; posting again a request it applied succeeds, so a retry stays one
request.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.api import CONTROL_REQUEST, SteerMode, StopMode, recorded_ending
from vs_project.api import INBOX_FORMAT, InboxClosedError, Project, ProjectError, RequestState

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TextIO

__all__ = ["control_main", "time_ordered_id"]

_NOT_FOUND = 1
_USAGE_ERROR = 2
_RUN_ENDED = 3
"""The run has ended: the request was not posted, or will not apply."""


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
    mode = steer.add_mutually_exclusive_group()
    mode.add_argument(
        "--standing",
        action="store_const",
        const=SteerMode.STANDING,
        dest="mode",
        help="give it to every turn of the target from now on, until withdrawn",
    )
    mode.add_argument(
        "--interrupt",
        action="store_const",
        const=SteerMode.INTERRUPT,
        dest="mode",
        help=(
            "offer it to a turn of the target running now, on a provider that takes "
            "messages mid-turn; otherwise the next turn gets it"
        ),
    )
    steer.set_defaults(mode=SteerMode.NEXT_TURN)
    withdraw = actions.add_parser(
        "withdraw", help="end standing guidance sent earlier as a standing steer"
    )
    withdraw.add_argument("withdraws", metavar="CONTROL_ID", help="the standing steer's id")
    actions.add_parser("park", help="finish agent turns, save and exit for relaunch")
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
    status = actions.add_parser(
        "status", help="say whether a request is pending, applied or rejected, and why"
    )
    status.add_argument("request_id", metavar="CONTROL_ID", help="the id the request was sent with")
    cancel_attempt = actions.add_parser(
        "cancel-attempt", help="stop one attempt; it closes cancelled and is never judged"
    )
    cancel_attempt.add_argument("attempt", metavar="ATTEMPT_ID", help="the attempt to cancel")
    cancel_item = actions.add_parser(
        "cancel-item", help="stop every attempt at one work item and start no new one at it"
    )
    cancel_item.add_argument("item", metavar="ITEM_ID", help="the work item to cancel")
    limit = actions.add_parser(
        "set-max-parallel", help="change how many attempts the run admits at once"
    )
    limit.add_argument("max_parallel", metavar="N", type=int, help="the new limit, at least 1")
    stop = actions.add_parser("stop", help="end the run")
    stop.add_argument(
        "--mode",
        choices=[mode.value for mode in StopMode],
        default=StopMode.DRAIN.value,
        help="drain the work in flight, or cancel it",
    )
    return parser


def _payload(arguments: argparse.Namespace, control_id: str) -> dict[str, object]:
    # A subcommand is the action's name with dashes: ``cancel-attempt`` is ``cancel_attempt``.
    action = arguments.action.replace("-", "_")
    common = {"control_id": control_id, "action": action}
    if action == "steer":
        kinds = ("role", "attempt", "item")
        named = [(kind, getattr(arguments, kind)) for kind in kinds]
        target = next(({"kind": k, "id": i} for k, i in named if i is not None), None)
        targeted = {"target": target} if target else {}
        return {**common, "text": arguments.text, "mode": arguments.mode.value, **targeted}
    # Every other action's options are its request's fields, under the same names.
    fields = {
        "withdraw": ("withdraws",),
        "resume": ("fallback", "when"),
        "stop": ("mode",),
        "cancel_attempt": ("attempt",),
        "cancel_item": ("item",),
        "set_max_parallel": ("max_parallel",),
    }.get(action, ())
    return {**common, **{name: getattr(arguments, name) for name in fields}}


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
    if arguments.action == "status":
        return _status(arguments, cwd=cwd, out=out, err=err)
    try:
        request = CONTROL_REQUEST.validate_python(
            _payload(arguments, arguments.control_id or new_control_id())
        )
        project = Project.open(arguments.project or cwd)
        project.state.load_run(arguments.run_id)
        project.state.require_local_run(arguments.run_id)
        inbox = project.state.control_inbox(arguments.run_id)
        inbox.post(
            request.control_id,
            request.model_dump_json().encode(),
            ended=lambda: recorded_ending(project, arguments.run_id),
        )
        served = inbox.served_format()
    except InboxClosedError as error:
        err.write(f"vibesys control: {error}\n")
        return _RUN_ENDED
    except (ValidationError, ProjectError) as error:
        err.write(f"vibesys control: {error}\n")
        return _USAGE_ERROR
    if served != INBOX_FORMAT:
        err.write(
            f"vibesys control: run {arguments.run_id!r} has not declared that it applies "
            "requests in posting order (it has not started yet, or runs an older VibeSys, "
            "which applies them in id order)\n"
        )
    out.write(f"{request.control_id}\n")
    return 0


def _status(arguments: argparse.Namespace, *, cwd: Path, out: TextIO, err: TextIO) -> int:
    """Print what became of one request: ``pending``, ``applied`` or ``rejected: <why>``."""
    try:
        project = Project.open(arguments.project or cwd)
        project.state.load_run(arguments.run_id)
        project.state.require_local_run(arguments.run_id)
        status = project.state.control_inbox(arguments.run_id).status(
            arguments.request_id, ended=recorded_ending(project, arguments.run_id)
        )
    except ProjectError as error:
        err.write(f"vibesys control: {error}\n")
        return _USAGE_ERROR
    if status is None:
        err.write(
            f"vibesys control: no request {arguments.request_id!r} was posted to run "
            f"{arguments.run_id!r}\n"
        )
        return _NOT_FOUND
    if status.state is RequestState.REJECTED:
        out.write(f"rejected: {status.reason}\n")
    else:
        out.write(f"{status.state.value}\n")
    return 0
