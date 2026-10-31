#!/usr/bin/env python3
"""Own optimization lifecycle state; standard library only, Python 3.10+.

Public API: main(argv, now, out, err, git), status(state, now), transition(state,
item, target, reason), finishable(state, now, records). Invalid operations raise
ValueError or return CLI exit 2. Only the lead writes state, never workers.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "measure/scripts"))
from measurements import InvalidInputError, compare_commit, load_records

if TYPE_CHECKING:
    from collections.abc import Sequence

State = dict[str, Any]


class Git(Protocol):
    """Host operations used by the controller shell."""

    def __call__(self, cwd: Path, args: list[str]) -> str: ...

    def alive(self, pid: int) -> bool: ...


__all__ = ["finishable", "main", "status", "transition"]

TERMINAL = {"won", "parked", "abandoned"}
EDGES = {
    "dispatched": {"returned", "abandoned"},
    "returned": {"judging", "parked", "abandoned"},
    "judging": TERMINAL,
}


def _date(value: str) -> datetime:
    return datetime.fromisoformat(
        value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    )


def _name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise ValueError(f"invalid name: {value}")
    return value


def _sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError(f"invalid commit: {value}")
    return value


def status(state: State, now: datetime) -> dict[str, Any]:
    """Return scheduling facts and the next action, without reading the clock."""
    opened = [i for i in state["items"].values() if i["state"] not in TERMINAL]
    busy = sum(i["state"] in {"dispatched", "judging"} for i in opened)
    free = max(0, state["fanout"] - busy)
    if state["finished"]:
        action = "finish succeeded"
    elif now >= _date(state["wrap_up_at"]):
        action = "begin wrap-up: close open items, stop owned tasks, measure main, run finish"
    elif any(i["state"] == "returned" for i in opened):
        action = "judge returned items, then choose work or record idle --reason"
    elif free and not state["idle_reason"]:
        action = f"dispatch {free} items with add, or record why not with idle --reason"
    else:
        action = "wait for completion notifications"
    return {
        "seconds_left": (_date(state["deadline"]) - now).total_seconds(),
        "wrap_up_at": state["wrap_up_at"],
        "open_items": opened,
        "free_slots": free,
        "next": action,
        "idle_reason": state["idle_reason"],
    }


def transition(state: State, item: str, target: str, reason: str) -> State:
    """Apply a legal work transition to a copy; terminal items cannot reopen."""
    if state["finished"]:
        raise ValueError("run already finished")
    result = copy.deepcopy(state)
    row = result["items"][item]
    if target not in EDGES.get(row["state"], set()) or not reason.strip():
        raise ValueError(f"invalid transition {row['state']} -> {target}, or empty reason")
    if target == "judging" and status(state, _date(state["started_at"]))["free_slots"] == 0:
        raise ValueError("no free judge slot")
    if target == "won" and not row.get("judge_pass"):
        raise ValueError("won requires a recorded judge pass")
    if target == "returned":
        result["idle_reason"] = None
    row.update(state=target, reason=reason)
    return result


def finishable(
    state: State,
    now: datetime,
    records: list[dict[str, Any]],
    *,
    early: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Check all finish guards, including a fresh successful measurement of current main."""
    if early and not reason.strip():
        raise ValueError("finish --early requires --reason")
    if now < _date(state["wrap_up_at"]) and not (early and reason.strip()):
        raise ValueError("too early: wrap-up starts at " + state["wrap_up_at"])
    if any(i["state"] not in TERMINAL for i in state["items"].values()):
        raise ValueError("open work items: collect or abandon them first")
    if state["commands"]:
        raise ValueError("stop owned background commands before finish")
    fresh = [
        r
        for r in records
        if r["commit"] == state["main_commit"]
        and _date(r["recorded_at"]) > _date(state["main_moved_at"])
        and r["check"] == "pass"
    ]
    if not fresh:
        raise ValueError("record a final successful measurement after main last moved")
    result = compare_commit(records, state["main_commit"], state["base_commit"])
    if result["ratio"] is None or result["check"] != "pass":
        raise ValueError("final measurement must compare with the base and pass correctness")
    return result


def read_state(run: Path) -> State:
    """Read the controller schema, rejecting unknown keys before consuming state."""
    state = json.loads((run / "run.json").read_text())
    fields = {
        "schema",
        "run_id",
        "task",
        "started_at",
        "deadline",
        "wrap_up_at",
        "fanout",
        "base_commit",
        "main_branch",
        "main_commit",
        "main_moved_at",
        "items",
        "commands",
        "finished",
        "pending_move",
        "promotions",
        "idle_reason",
        "decisions",
    }
    if not isinstance(state, dict) or set(state) != fields:
        raise ValueError(f"run.json keys must be {sorted(fields)}; got {state}")
    if state["schema"] != 1 or not isinstance(state["fanout"], int) or state["fanout"] <= 0:
        raise ValueError("run.json: invalid schema or fanout")
    for key in ("started_at", "deadline", "wrap_up_at", "main_moved_at"):
        if _date(state[key]).tzinfo is None:
            raise ValueError(f"run.json: {key} needs timezone")
    for row in state["items"].values():
        if row["state"] not in {*EDGES, *TERMINAL}:
            raise ValueError("run.json: unknown item state")
    return state


def _save(run: Path, state: State) -> None:
    path = run / "run.json"
    temporary = run / "run.json.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)
    rows = ["| id | kind | hypothesis | state | reason |", "| --- | --- | --- | --- | --- |"]
    for key, item in state["items"].items():
        fields = [key, item["kind"], item["hypothesis"], item["state"], item["reason"]]
        rows.append(
            "| "
            + " | ".join(str(v).replace("|", "&#124;").replace("\n", " ") for v in fields)
            + " |"
        )
    rows.extend(["", "## Decisions", "", json.dumps(state["decisions"], indent=2)])
    (run / "ledger.md").write_text("\n".join(rows) + "\n")


def _start(args: argparse.Namespace, now: datetime, git: Git) -> Path:
    root = Path(git(Path.cwd(), ["rev-parse", "--show-toplevel"]).strip())
    home = root / ".vibesys-optimize"
    task = _name(args.task)
    if args.seconds <= 0 or args.fanout <= 0:
        raise ValueError("seconds and fanout must be positive")
    if (home / "active").exists():
        raise ValueError("an active run already exists")
    if git(root, ["status", "--porcelain", "--untracked-files=no"]).strip():
        raise ValueError("tracked files must be clean")
    base = _sha(git(root, ["rev-parse", "HEAD"]).strip())
    run_id = now.strftime("%Y%m%d-%H%M%S") + "-" + task
    run = home / run_id
    run.mkdir(parents=True)
    (run / "notes").mkdir()
    (run / "worktrees").mkdir()
    branch = "vibesys/" + run_id
    state = {
        "schema": 1,
        "run_id": run_id,
        "task": task,
        "started_at": now.isoformat(),
        "deadline": (now + timedelta(seconds=args.seconds)).isoformat(),
        "wrap_up_at": (
            now + timedelta(seconds=args.seconds - min(1200, args.seconds / 4))
        ).isoformat(),
        "fanout": args.fanout,
        "base_commit": base,
        "main_branch": branch,
        "main_commit": base,
        "main_moved_at": now.isoformat(),
        "items": {},
        "commands": {},
        "finished": False,
        "pending_move": None,
        "promotions": [],
        "idle_reason": None,
        "decisions": [],
    }
    exclude = Path(git(root, ["rev-parse", "--git-path", "info/exclude"]).strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    text = exclude.read_text() if exclude.exists() else ""
    if ".vibesys-optimize/" not in text.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(text + "\n.vibesys-optimize/\n")
    _save(run, state)
    git(root, ["worktree", "add", "-b", branch, str(run / "worktrees/main"), base])
    (home / "active").write_text(run_id + "\n")
    return run


def _move(run: Path, state: State, item: str, now: datetime, git: Git) -> None:
    row = state["items"][item]
    commit = _sha(row.get("commit", ""))
    comparison = compare_commit(load_records(run), commit, state["main_commit"])
    if (
        row["state"] != "won"
        or not row.get("judge_pass")
        or comparison["verdict"] != "better"
        or comparison["check"] != "pass"
    ):
        raise ValueError(
            "promotion requires won, judge pass, check pass and better against current main"
        )
    worktree = run / "worktrees/main"
    if git(worktree, ["status", "--porcelain"]).strip():
        raise ValueError("main worktree must be clean")
    if git(worktree, ["rev-parse", "HEAD"]).strip() != state["main_commit"]:
        raise ValueError("main moved outside run.py")
    state["pending_move"] = {"commit": commit, "comparison": comparison}
    _save(run, state)
    git(worktree, ["reset", "--hard", commit])
    state["promotions"].append(
        {"commit": commit, "comparison": comparison, "moved_at": now.isoformat()}
    )
    state.update(
        idle_reason=None, main_commit=commit, main_moved_at=now.isoformat(), pending_move=None
    )


def _recover(run: Path, state: State, now: datetime, git: Git) -> None:
    pending = state["pending_move"]
    if pending:
        head = git(run / "worktrees/main", ["rev-parse", "HEAD"]).strip()
        if head not in {state["main_commit"], pending["commit"]}:
            raise ValueError("unexpected main head during recovery")
        if head == pending["commit"]:
            state["promotions"].append({**pending, "moved_at": now.isoformat()})
            state.update(idle_reason=None, main_commit=head, main_moved_at=now.isoformat())
        state["pending_move"] = None
        _save(run, state)


def _item_command(args: argparse.Namespace, state: State, now: datetime) -> State:
    command = args.command
    if command == "add":
        _name(args.id)
        if now >= _date(state["wrap_up_at"]) or not status(state, now)["free_slots"]:
            raise ValueError("no dispatch slot or wrap-up has begun")
        if args.id in state["items"]:
            raise ValueError("duplicate item id")
        state["items"][args.id] = {
            "kind": args.kind,
            "hypothesis": args.hypothesis,
            "state": "dispatched",
            "reason": "dispatched",
        }
    elif command == "transition":
        state = transition(state, args.id, args.to, args.reason)
    elif command == "judge":
        row = state["items"][args.id]
        if row["state"] != "judging" or not args.reason.strip():
            raise ValueError("judge needs judging state and reason")
        row.update(
            commit=_sha(args.commit), judge_pass=args.result == "pass", judge_reason=args.reason
        )
    return state


def _finish(
    run: Path, state: State, now: datetime, git: Git, args: argparse.Namespace
) -> dict[str, Any]:
    head = git(run / "worktrees/main", ["rev-parse", "HEAD"]).strip()
    if head != state["main_commit"]:
        raise ValueError("main moved outside run.py")
    records = load_records(run)
    comparison = finishable(state, now, records, early=args.early, reason=args.reason)
    if args.early:
        _decision(state, "early-finish", args.reason, now)
    record_ids = [
        index
        for index, row in enumerate(records, 1)
        if row["commit"] in {state["base_commit"], state["main_commit"]}
        and (row["metric"], row["evaluator"]) == (comparison["metric"], comparison["evaluator"])
    ]
    (run / "report.md").write_text(
        "# Result\n\n```json\n"
        + json.dumps(comparison, indent=2)
        + "\n```\n\n"
        + "Record ids (1-based measurements.jsonl record ordinals): "
        + json.dumps(record_ids)
        + "\n\nDecisions:\n"
        + json.dumps(state["decisions"], indent=2)
        + "\n"
    )
    state["finished"] = True
    return comparison


def _operate(
    args: argparse.Namespace,
    run: Path,
    state: State,
    now: datetime,
    git: Git,
) -> dict[str, Any]:
    command = args.command
    if state["finished"] and command != "status":
        raise ValueError("run already finished")
    if command in {"add", "transition", "judge"}:
        state = _item_command(args, state, now)
    elif command == "move-main":
        _move(run, state, args.id, now, git)
    elif command in {"track", "untrack"}:
        _command(args, state, git)
    elif command == "idle":
        _decision(state, "idle", args.reason, now)
        state["idle_reason"] = args.reason
    elif command == "finish":
        if any(git.alive(row["pid"]) for row in state["commands"].values()):
            raise ValueError("tracked process still alive")
        comparison = _finish(run, state, now, git, args)
    _save(run, state)
    if state["finished"]:
        active = run.parent / "active"
        if active.exists() and active.read_text().strip() == state["run_id"]:
            active.unlink()
    return comparison if command == "finish" else status(state, now)


def _command(args: argparse.Namespace, state: State, git: Git) -> None:
    if args.command == "track":
        if args.id in state["commands"] or args.pid <= 0:
            raise ValueError("duplicate command or nonpositive pid")
        state["commands"][args.id] = {"description": args.description, "pid": args.pid}
    else:
        if git.alive(state["commands"][args.id]["pid"]):
            raise ValueError("command still running; stop it and confirm completion first")
        del state["commands"][args.id]


def _decision(state: State, kind: str, reason: str, now: datetime) -> None:
    if not reason.strip():
        raise ValueError("a written reason is required")
    state["decisions"].append({"kind": kind, "reason": reason, "at": now.isoformat()})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    start = subs.add_parser("start")
    start.add_argument("--task", required=True)
    start.add_argument("--seconds", type=int, required=True)
    start.add_argument("--fanout", type=int, default=3)
    for name in (
        "status",
        "add",
        "transition",
        "judge",
        "move-main",
        "track",
        "untrack",
        "idle",
        "finish",
    ):
        sub = subs.add_parser(name)
        sub.add_argument("--run", type=Path, required=True)
        if name in {"add", "transition", "judge", "move-main", "track", "untrack"}:
            sub.add_argument("--id", required=True)
        if name == "add":
            sub.add_argument(
                "--kind", choices=("implement", "profile", "combine", "judge"), required=True
            )
            sub.add_argument("--hypothesis", required=True)
        if name == "transition":
            sub.add_argument("--to", choices=tuple(EDGES) + tuple(sorted(TERMINAL)), required=True)
        if name in {"transition", "judge", "idle"}:
            sub.add_argument("--reason", required=True)
        if name == "judge":
            sub.add_argument("--commit", required=True)
            sub.add_argument("--result", choices=("pass", "fail"), required=True)
        if name == "track":
            sub.add_argument("--description", required=True)
            sub.add_argument("--pid", type=int, required=True)
        if name == "finish":
            sub.add_argument("--early", action="store_true")
            sub.add_argument("--reason", default="")
    return parser


def main(
    argv: Sequence[str],
    *,
    now: datetime,
    out: TextIO,
    err: TextIO,
    git: Git,
) -> int:
    """Execute one command with an explicit instant and injected git command runner."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "start":
            out.write(str(_start(args, now, git)) + "\n")
            return 0
        run = args.run.resolve()
        state = read_state(run)
        _recover(run, state, now, git)
        result = _operate(args, run, state, now, git)
        out.write(json.dumps(result, indent=2) + "\n")
    except (ValueError, KeyError, OSError, TypeError, AttributeError, InvalidInputError) as error:
        err.write(f"error: {error}\n")
        return 2
    return 0


class HostGit:
    """Git and PID inspection for the standalone installed controller."""

    def __call__(self, cwd: Path, args: list[str]) -> str:
        # LW-100348 [S603]; git argv is controller-owned and never uses a shell. Separate
        # wrappers for each git verb would duplicate this single I/O boundary.
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr)
        return result.stdout

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1:],
            now=datetime.now(timezone.utc),
            out=sys.stdout,
            err=sys.stderr,
            git=HostGit(),
        )
    )
