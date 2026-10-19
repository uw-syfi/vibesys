"""``vibesys evals``: list and remove the evaluations in a project's ledger on the cluster.

The evaluation ledger (#2757) records every Slurm evaluation a project's runs
asked for, under the operator's staging root on the cluster. ``list`` reads it
with no VibeSys run attached; ``remove`` withdraws entries: a queued one never
runs, a running one has its Slurm job cancelled, and a run attached to it
submits the request again as a new attempt. Both go through the ledger and cluster interfaces of
``vs_slurm``, never through the files.

This is a light host command: its module-level imports stay within the
allowlist ``tests/architecture/test_light_entrypoints.py`` enforces.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from vs_project.api import Project, ProjectError
from vs_sim.api import OsProcesses, SystemClock
from vs_slurm.api import (
    EntryPhase,
    Kept,
    LedgerError,
    LedgerListing,
    LedgerRow,
    NotInLedger,
    RemoteEvaluationLedger,
    Resolved,
    SlurmCluster,
    SlurmConfigError,
    SlurmJobRunner,
    WithdrawalTiming,
    Withdrawer,
    Withdrawn,
    WithdrawOutcome,
    WithdrawUnknown,
    entry_id,
    list_entries,
    load_slurm_config,
    resolve_entries,
    validate_project,
    withdraw,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Clock
    from vs_slurm.api import Cluster, EvaluationLedger

_DEFAULT_SLURM_CONFIG = Path("~/.config/vibesys/slurm.toml")
_REMOVED_REASON = "removed with `vibesys evals remove`"
# The withdrawer this command names in the entries it marks withdrawing.
_WITHDRAWER = "vibesys-evals"


@dataclass(frozen=True, slots=True)
class EvalsHost:
    """What the commands reach: the project's ledger, the cluster, the clock and the operator.

    ``ledger`` opens the ledger of a project id. ``current_project`` names the
    project of the working directory, for a command without ``--project``.
    ``confirm`` asks the operator a yes-or-no question. ``call_seconds``
    bounds one transport call to the cluster (``transport_timeout_seconds``).
    """

    ledger: Callable[[str], EvaluationLedger]
    cluster: Cluster
    clock: Clock
    call_seconds: float
    current_project: Callable[[], str]
    confirm: Callable[[str], bool]


def _project(value: str) -> str:
    try:
        return validate_project(value)
    except LedgerError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys evals",
        description="List and remove the Slurm evaluations in a project's evaluation ledger.",
    )
    parser.add_argument(
        "--slurm-config",
        type=Path,
        default=_DEFAULT_SLURM_CONFIG,
        help="the operator's Slurm settings file (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list the project's evaluations")
    listing.add_argument("--project", type=_project, help="a project id (default: this project)")
    listing.add_argument(
        "--state",
        choices=[phase.value for phase in EntryPhase],
        help="only entries in this state",
    )
    listing.add_argument("--json", action="store_true", help="print one JSON document")
    remove = commands.add_parser("remove", help="withdraw evaluations from the ledger")
    remove.add_argument("ids", nargs="*", metavar="id", help="entry ids, as `list` shows them")
    remove.add_argument("--all", action="store_true", help="withdraw every live entry")
    remove.add_argument("--project", type=_project, help="a project id (default: this project)")
    remove.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


def run(argv: list[str], host: EvalsHost) -> tuple[int, str]:
    """Run one ``vibesys evals`` command; return its exit code and stdout text."""
    parser = _parser()
    args = parser.parse_args(argv)
    project = args.project or host.current_project()
    ledger = host.ledger(project)
    listing = list_entries(ledger, host.cluster, project=project, now=host.clock.now())
    if args.command == "list":
        if args.state is not None:
            rows = tuple(row for row in listing.rows if row.phase.value == args.state)
            listing = listing.model_copy(update={"rows": rows})
        return 0, (listing.model_dump_json() if args.json else format_listing(listing)) + "\n"
    if args.all == bool(args.ids):
        parser.error("name the entries to remove, or pass --all")
    return _remove(args, host, ledger, listing)


def _remove(
    args: argparse.Namespace, host: EvalsHost, ledger: EvaluationLedger, listing: LedgerListing
) -> tuple[int, str]:
    live = tuple(row for row in listing.rows if row.phase is not EntryPhase.WITHDRAWN)
    if args.all:
        keys = tuple(row.key for row in live)
    else:
        resolved = resolve_entries(tuple(row.key for row in listing.rows), tuple(args.ids))
        if not isinstance(resolved, Resolved):
            problems = [f"no entry {given}" for given in resolved.unknown]
            problems += [f"{given} names more than one entry" for given in resolved.ambiguous]
            return 1, "; ".join(problems) + "\n"
        keys = resolved.keys
    if not keys:
        return 0, f"Project {listing.project} has no live evaluations to remove.\n"
    # One withdrawer, so one writer id, for the whole command, as a run keeps one. Built
    # before the question: a transport timeout whose mark is over the bound is refused
    # (``WithdrawalTiming``) before the operator is asked to confirm a removal it cannot do.
    remover = Withdrawer(
        by=_WITHDRAWER,
        reason=_REMOVED_REASON,
        timing=WithdrawalTiming(clock=host.clock, call_seconds=host.call_seconds),
    )
    targets = [row for row in listing.rows if row.key in keys]
    question = (
        f"Withdraw {len(keys)} evaluation(s) from project {listing.project}?\n"
        + "\n".join(_line(row) for row in targets)
        + "\nQueued jobs will never run; running jobs are cancelled."
    )
    if not args.yes and not host.confirm(question):
        return 1, "Nothing removed.\n"
    outcomes = [withdraw(ledger, host.cluster, key, remover) for key in keys]
    code = 1 if any(isinstance(outcome, WithdrawUnknown) for outcome in outcomes) else 0
    return code, "\n".join(format_withdrawal(outcome) for outcome in outcomes) + "\n"


def format_listing(listing: LedgerListing) -> str:
    """Render a listing for a terminal, one entry per line."""
    if not listing.rows and not listing.unreadable:
        return f"Project {listing.project} has no evaluations in its ledger."
    lines = [f"Project {listing.project}:"]
    lines.extend(_line(row) for row in listing.rows)
    lines.extend(f"unreadable entry {problem}" for problem in listing.unreadable)
    return "\n".join(lines)


def _line(row: LedgerRow) -> str:
    lease = (
        "no lease"
        if row.lease_age_seconds is None
        else f"lease renewed {_duration(row.lease_age_seconds)} ago"
    )
    if row.unreadable_leases:
        lease += (
            f", unreadable lease from {', '.join(row.unreadable_leases)}"
            f" (`vibesys evals remove {row.entry}` withdraws it)"
        )
    wait = ""
    if row.waiting_seconds is not None:
        wait = f"  waiting {_duration(row.waiting_seconds)}"
        if row.pending_reason:
            wait += f" ({row.pending_reason})"
        if row.estimated_start:
            wait += f", starts ~{row.estimated_start}"
    job = f"job {row.job_id}" if row.job_id is not None else "no job"
    labels = f"{row.candidate or '-'} [{row.workstream or '-'}]"
    result = f"  results {row.result_location}" if row.result_location else ""
    detail = f"  ({row.detail})" if row.detail else ""
    return (
        f"  {row.entry}  {row.phase.value:<9}  {job}  run {row.owner}, {lease}  "
        f"{labels}{wait}{result}{detail}"
    )


def _duration(seconds: float) -> str:
    whole = int(seconds)
    if whole < 60:  # noqa: PLR2004  # lint-waiver: LW-275702 [PLR2004]; seconds per minute, read as such beside the hour constant below.
        return f"{whole}s"
    minutes, _ = divmod(whole, 60)
    if minutes < 60:  # noqa: PLR2004  # lint-waiver: LW-275703 [PLR2004]; minutes per hour, read as such beside the minute constant above.
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_withdrawal(outcome: WithdrawOutcome) -> str:
    """Render one removal: what was withdrawn, and which Slurm job was cancelled."""
    match outcome:
        case Withdrawn(key=key, already=True):
            return f"{entry_id(key)}: already withdrawn"
        case Withdrawn(key=key, cancelled_job=None):
            return f"{entry_id(key)}: withdrawn; no queued or running job to cancel"
        case Withdrawn(key=key, cancelled_job=job):
            return f"{entry_id(key)}: withdrawn; cancelled Slurm job {job}"
        case Kept(key=key):
            return f"{entry_id(key)}: kept; a run leased it or its job finished meanwhile"
        case NotInLedger(key=key):
            return f"{entry_id(key)}: not in the ledger"
        case WithdrawUnknown(key=key, reason=reason):
            return f"{entry_id(key)}: not withdrawn, try again: {reason}"
        case _:
            assert_never(outcome)


def _current_project() -> str:
    return Project.discover(Path.cwd()).state.load_project().project_id


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        sys.stderr.write(f"{question}\nRefusing without a terminal; pass --yes to confirm.\n")
        return False
    sys.stderr.write(f"{question}\nProceed? [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    """Run ``vibesys evals`` against the operator's cluster."""
    arguments = sys.argv[1:] if argv is None else argv
    config_path = _parser().parse_known_args(arguments)[0].slurm_config.expanduser()
    try:
        config = load_slurm_config(config_path)
    except SlurmConfigError as exc:
        sys.stderr.write(f"vibesys evals: {exc}\n")
        return 1
    processes = OsProcesses()
    clock = SystemClock()
    with tempfile.TemporaryDirectory(prefix="vibesys-evals-") as scratch:
        state_root = Path(scratch)
        cluster = SlurmCluster(
            SlurmJobRunner(config, scratch_root=state_root, processes=processes, clock=clock),
            state_root=state_root,
        )
        host = EvalsHost(
            ledger=lambda project: RemoteEvaluationLedger(
                config, project=project, processes=processes, clock=clock, scratch_root=state_root
            ),
            cluster=cluster,
            clock=clock,
            call_seconds=config.transport_timeout_seconds,
            current_project=_current_project,
            confirm=_ask,
        )
        try:
            code, output = run(arguments, host)
        except (LedgerError, ProjectError) as exc:
            sys.stderr.write(f"vibesys evals: {exc}\n")
            return 1
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
