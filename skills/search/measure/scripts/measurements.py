#!/usr/bin/env python3
"""Record and compare benchmark measurements of commits for one optimization.

The single writer of an optimization's ``measurements/<commit-sha>.jsonl`` shards. It uses only
the Python standard library (3.10 or newer) and runs from an installed Claude Code plugin,
outside any VibeSys package, so nothing here may import one.

Subcommands use ``--dir DIR`` (the state directory ``.vibesys-state/optimize/<task>``), except
``run``. The task and the repository root follow from that path.

* ``run -- <command>``: execute any benchmark load under the host-wide lock.
* ``record``: validate one measurement and append it to its commit's shard.
* ``compare``: a commit's samples against the base's.
* ``best``: the commit with the best verdict-backed result.
* ``show``: the records of all shards.
* ``check [--branch B]``: read-only; does the branch head have a passing, ``better`` record?

``--json`` makes ``compare``, ``best``, ``show`` and ``check`` print JSON. Exit codes: 0 ok,
1 ``check`` found the head unproven, 2 invalid input.

The comparison is VibeSys's rule for a task that declares no noise: Welch's t test on each
side's own spread. A verdict needs three samples on each side that are not all alike; the bar is
the Student-t 97.5% quantile at the floor of the Welch-Satterthwaite degrees of freedom (4.30
noises at the fewest samples, 2.04 with many). Anything less is ``inconclusive``, never
``within_noise``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import TextIO

__all__ = [
    "Comparison",
    "InvalidInputError",
    "compare_commit",
    "compare_samples",
    "load_records",
    "main",
    "task_fingerprint",
]

SCHEMA_VERSION = 1
MEASUREMENTS_DIR = "measurements"
STATE_PARENT = (".vibesys-state", "optimize")
MIN_SAMPLES = 3
"""The fewest samples a side needs before a verdict is possible."""

Verdict = Literal["better", "within_noise", "worse", "inconclusive"]
Direction = Literal["max", "min"]

_T_FIRST_DEGREES = MIN_SAMPLES - 1
_T_975 = (
    4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip
"""The two-sided 95% Student-t quantile for 2 to 30 degrees of freedom, to three decimals.

Beyond 30 degrees the last entry is used, slightly above the normal 1.96, so a verdict is
never given that the exact quantile would withhold. VibeSys uses the same table."""
_DEGREES_SLACK = 1e-9
"""Added before the floor: degrees that are whole in exact arithmetic can come out a few
units in the last place below the whole number."""

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FIELDS = (
    "schema",
    "recorded_at",
    "host",
    "commit",
    "role",
    "label",
    "metric",
    "direction",
    "command",
    "samples",
    "check",
    "exclusive",
    "evaluator",
)
_ROLES = ("base", "candidate")
_DIRECTIONS = ("max", "min")
_CHECKS = ("pass", "fail")
_YES_NO = ("yes", "no")
# Match vs-project input exclusions without importing the installed package.
_SKIPPED_PARTS = (
    "__pycache__",
    ".cache",
    ".git",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "agent.toml",
    "node_modules",
    "results.json",
)


class InvalidInputError(Exception):
    """The arguments or the recorded data are not valid; the message names the offender."""


@dataclass(frozen=True)
class Comparison:
    """One side's samples against the other's.

    ``ratio`` is oriented so that above 1 is better (candidate over base for ``max``, base over
    candidate for ``min``); ``raw_ratio`` is candidate mean over base mean. ``noise`` is one
    standard error of ``raw_ratio``.
    """

    verdict: Verdict
    candidate_mean: float
    base_mean: float
    ratio: float | None
    raw_ratio: float | None
    noise: float
    candidate_samples: int
    base_samples: int
    reason: str | None = None


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _squares(values: Sequence[float], mean: float) -> float:
    """The sum of squared relative deviations: exactly 0 for samples that are all alike."""
    if min(values) == max(values):
        return 0.0
    return sum(((x - mean) / mean) ** 2 for x in values)


def _variance(values: Sequence[float], mean: float) -> float:
    free = len(values) - 1
    if free == 0 or mean == 0:
        return 0.0
    return _squares(values, mean) / free


def _welch(
    mine: Sequence[float], c: float, theirs: Sequence[float], b: float
) -> tuple[float, float]:
    """The relative standard error of the ratio and its Welch-Satterthwaite degrees of freedom."""
    parts = tuple(
        (_variance(values, mean) / len(values), len(values) - 1)
        for values, mean in ((mine, c), (theirs, b))
    )
    total = sum(part for part, _ in parts)
    if total == 0:
        return 0.0, 0.0
    return math.sqrt(total), total**2 / sum(part**2 / free for part, free in parts if part > 0)


def _t_critical(degrees: float) -> float:
    whole = math.floor(degrees + _DEGREES_SLACK)
    if whole < _T_FIRST_DEGREES:
        return math.inf
    return _T_975[min(whole - _T_FIRST_DEGREES, len(_T_975) - 1)]


def compare_samples(
    candidate: Sequence[float], base: Sequence[float], direction: Direction
) -> Comparison:
    """Compare a candidate's samples with the base's under Welch's t test.

    Both sides must be non-empty (the caller guarantees it).
    """
    c, b = _mean(candidate), _mean(base)
    m, n = len(candidate), len(base)
    if b == 0:
        return Comparison(
            "inconclusive", c, b, None, None, 0.0, m, n, "the base mean is zero: no ratio exists"
        )
    raw = c / b
    error, degrees = _welch(candidate, c, base, b)
    noise = abs(raw) * error
    oriented = raw if direction == "max" else (1 / raw if raw != 0 else None)
    reason = None
    if min(m, n) < MIN_SAMPLES:
        verdict: Verdict = "inconclusive"
        reason = f"a verdict needs at least {MIN_SAMPLES} samples on each side ({m} vs {n})"
    elif noise <= 0:
        verdict = "inconclusive"
        reason = "the samples on both sides are alike, so they show no noise to judge against"
    else:
        gain = raw - 1 if direction == "max" else 1 - raw
        bar = _t_critical(degrees) * noise
        verdict = "better" if gain > bar else "worse" if gain < -bar else "within_noise"
    return Comparison(verdict, c, b, oriented, raw, noise, m, n, reason)


def task_fingerprint(task_dir: Path) -> str:
    """The SHA-256 of a task directory's file names and contents, in a fixed order."""
    digest = hashlib.sha256()
    files = sorted(
        p
        for p in task_dir.rglob("*")
        if p.is_file()
        and p.suffix not in {".pyc", ".pyo"}
        and p.relative_to(task_dir).parts[:2] != (".vibesys", "state")
        and not any(
            part in _SKIPPED_PARTS or part == ".env" or part.startswith(".env.")
            for part in p.relative_to(task_dir).parts
        )
    )
    for path in files:
        digest.update(path.relative_to(task_dir).as_posix().encode())
        digest.update(b"\0")
        content = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                content.update(block)
        digest.update(content.digest())
    return digest.hexdigest()


# --- records -----------------------------------------------------------------------------------


def _validate_commit(commit: object) -> str:
    if not isinstance(commit, str):
        raise InvalidInputError("commit must be a string")
    if "dirty" in commit:
        raise InvalidInputError(
            f"commit {commit!r} carries a dirty-tree marker: commit the change, then measure that"
        )
    if not _COMMIT.fullmatch(commit):
        raise InvalidInputError(
            f"commit {commit!r} is not a full lowercase hex SHA (40 or 64 digits): "
            "use `git rev-parse HEAD`"
        )
    return commit


def _validate_choice(record: dict[str, Any], key: str, allowed: tuple[str, ...]) -> None:
    if record[key] not in allowed:
        raise InvalidInputError(f"{key} must be one of {', '.join(allowed)}, not {record[key]!r}")


def _validate_text(record: dict[str, Any], key: str) -> None:
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"{key} must be a non-empty string")


def _validate_samples(samples: object) -> None:
    if not isinstance(samples, list) or not samples:
        raise InvalidInputError("samples must be a non-empty list of numbers")
    for value in samples:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidInputError(f"sample {value!r} is not a number")
        if not math.isfinite(value):
            raise InvalidInputError(f"sample {value!r} is not finite")


def validate_record(record: object) -> dict[str, Any]:
    """Return ``record`` if it is a valid measurement record, else raise ``InvalidInputError``."""
    if not isinstance(record, dict):
        raise InvalidInputError("a record must be a JSON object")
    unknown = sorted(set(record) - set(_FIELDS) - {"observations"})
    missing = sorted(set(_FIELDS) - set(record))
    if unknown:
        raise InvalidInputError(f"unknown field {', '.join(unknown)}")
    if missing:
        raise InvalidInputError(f"missing field {', '.join(missing)}")
    if record["schema"] != SCHEMA_VERSION:
        raise InvalidInputError(f"schema must be {SCHEMA_VERSION}, not {record['schema']!r}")
    _validate_commit(record["commit"])
    _validate_choice(record, "role", _ROLES)
    _validate_choice(record, "direction", _DIRECTIONS)
    if record["check"] is not None:
        _validate_choice(record, "check", _CHECKS)
    if record["exclusive"] is not None:
        _validate_choice(record, "exclusive", _YES_NO)
    for key in ("recorded_at", "host", "label", "metric", "command", "evaluator"):
        _validate_text(record, key)
    _validate_samples(record["samples"])
    _validate_timestamp(record["recorded_at"])
    _validate_observations(record)
    return record


def _validate_timestamp(value: str) -> None:
    try:
        stamp = datetime.fromisoformat(
            value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        raise InvalidInputError("recorded_at must be an ISO timestamp with timezone") from error
    if stamp.tzinfo is None:
        raise InvalidInputError("recorded_at needs timezone")


def _validate_observations(record: dict[str, Any]) -> None:
    observations = record.get("observations", {})
    if not isinstance(observations, dict):
        raise InvalidInputError("observations must map metric names to numeric samples")
    for name, values in observations.items():
        if not isinstance(name, str) or not name.strip() or name == record["metric"]:
            raise InvalidInputError(
                "observation names must be nonempty and distinct from the primary metric"
            )
        _validate_samples(values)


def load_records(state: Path) -> list[dict[str, Any]]:
    """Read validated records of every shard, oldest first; malformed lines name file and line."""
    records = []
    for path in sorted((state / MEASUREMENTS_DIR).glob("*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = validate_record(json.loads(line))
            except (ValueError, InvalidInputError) as error:
                raise InvalidInputError(f"{path} line {number}: {error}") from error
            if path.stem != record["commit"]:
                raise InvalidInputError(f"{path} line {number}: record is for {record['commit']}")
            records.append(record)
    return sorted(records, key=lambda r: r["recorded_at"])  # stable: ties keep shard order


def _check_against_history(new: dict[str, Any], history: list[dict[str, Any]]) -> None:
    bases = {r["commit"] for r in history if r["role"] == "base"}
    if new["role"] == "base" and bases and new["commit"] not in bases:
        raise InvalidInputError(
            f"the base is {sorted(bases)[0]}; a second base commit is not allowed"
        )
    if new["role"] == "candidate" and new["commit"] in bases:
        raise InvalidInputError("this is the base commit: record it with --role base")
    for record in history:
        same_metric = (record["metric"], record["evaluator"]) == (
            new["metric"],
            new["evaluator"],
        )
        if same_metric and record["direction"] != new["direction"]:
            raise InvalidInputError(
                f"metric {new['metric']!r} was recorded with direction {record['direction']}"
            )


def _repo_root(state: Path) -> Path:
    if state.parent.parts[-2:] != STATE_PARENT:
        raise InvalidInputError(
            f"{state} is not a state directory: expected <repo>/{'/'.join(STATE_PARENT)}/<task>"
        )
    return state.parents[2]


def _task_dir(state: Path) -> Path:
    directory = _repo_root(state) / ".vibesys" / "tasks" / state.name
    if not directory.is_dir():
        raise InvalidInputError(f"the task directory {directory} does not exist")
    return directory


def _record_from(
    args: argparse.Namespace, state: Path, host: str, now: Callable[[], datetime]
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "recorded_at": now().isoformat(timespec="seconds"),
        "host": host,
        "commit": args.commit,
        "role": args.role,
        "label": args.label,
        "metric": args.metric,
        "direction": args.direction,
        "command": args.command,
        "samples": args.sample,
        "check": args.check,
        "exclusive": args.exclusive,
        "evaluator": task_fingerprint(_task_dir(state)),
        "observations": json.loads(args.observations.read_text()) if args.observations else {},
    }


# --- commands ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Env:
    out: TextIO
    now: Callable[[], datetime]
    host: str


def _commit_check(records: list[dict[str, Any]]) -> str:
    """``fail`` if any record failed the correctness check, ``pass`` if one passed, else unknown."""
    checks = [r["check"] for r in records]
    if "fail" in checks:
        return "fail"
    return "pass" if "pass" in checks else "unknown"


def compare_commit(records: list[dict[str, Any]], commit: str, base: str | None) -> dict[str, Any]:
    """Compare recorded commits, rejecting missing evidence."""
    base_commit = base or next((r["commit"] for r in records if r["role"] == "base"), None)
    if base_commit is None:
        raise InvalidInputError("no base measurement is recorded: measure the base first")
    mine = [r for r in records if r["commit"] == commit]
    theirs = [r for r in records if r["commit"] == base_commit]
    if not mine:
        raise InvalidInputError(f"no measurement is recorded for commit {commit}")
    if not theirs:
        raise InvalidInputError(f"no measurement is recorded for base {base_commit}")
    anchor = theirs[-1]
    key = (anchor["metric"], anchor["evaluator"])
    comparable = [r for r in mine if (r["metric"], r["evaluator"]) == key]
    result: dict[str, Any] = {
        "commit": commit,
        "base": base_commit,
        "metric": anchor["metric"],
        "direction": anchor["direction"],
        "evaluator": anchor["evaluator"],
        "check": _commit_check(mine),
        "observed_ranges": {
            "candidate": _observed_ranges(comparable),
            "base": _observed_ranges([r for r in theirs if (r["metric"], r["evaluator"]) == key]),
        },
        "warnings": _warnings(
            comparable, [r for r in theirs if (r["metric"], r["evaluator"]) == key]
        ),
    }
    if not comparable:
        result.update(_uncompared("the commit's samples read another metric or evaluator"))
        return result
    comparison = compare_samples(
        [s for r in comparable for s in r["samples"]],
        [s for r in theirs if (r["metric"], r["evaluator"]) == key for s in r["samples"]],
        anchor["direction"],
    )
    result.update(_comparison_fields(comparison))
    return result


def _observed_ranges(records: list[dict[str, Any]]) -> dict[str, list[float]]:
    pooled: dict[str, list[float]] = {}
    for record in records:
        for name, values in {
            record["metric"]: record["samples"],
            **record.get("observations", {}),
        }.items():
            pooled.setdefault(name, []).extend(values)
    return {name: [min(values), max(values)] for name, values in pooled.items()}


def _uncompared(reason: str) -> dict[str, Any]:
    return {
        "verdict": "inconclusive",
        "ratio": None,
        "raw_ratio": None,
        "noise": None,
        "candidate_mean": None,
        "base_mean": None,
        "candidate_samples": 0,
        "base_samples": 0,
        "reason": reason,
    }


def _comparison_fields(comparison: Comparison) -> dict[str, Any]:
    return {
        "verdict": comparison.verdict,
        "ratio": comparison.ratio,
        "raw_ratio": comparison.raw_ratio,
        "noise": comparison.noise,
        "candidate_mean": comparison.candidate_mean,
        "base_mean": comparison.base_mean,
        "candidate_samples": comparison.candidate_samples,
        "base_samples": comparison.base_samples,
        "reason": comparison.reason,
    }


def _warnings(mine: list[dict[str, Any]], theirs: list[dict[str, Any]]) -> list[str]:
    warnings = []
    hosts = {r["host"] for r in (*mine, *theirs)}
    if len(hosts) > 1:
        warnings.append(f"the samples were taken on different hosts: {', '.join(sorted(hosts))}")
    if any(r["exclusive"] == "no" for r in (*mine, *theirs)):
        warnings.append("some samples were taken on a host shared with other work")
    return warnings


def _format_comparison(result: dict[str, Any]) -> str:
    head = f"{result['commit'][:12]} vs base {result['base'][:12]}: {result['verdict']}"
    if result["ratio"] is None:
        detail = f" ({result['reason']})"
    else:
        detail = (
            f" (ratio {result['ratio']:.4f}, noise {result['noise']:.4f}, "
            f"{result['candidate_samples']} vs {result['base_samples']} samples"
        )
        detail += f"; {result['reason']})" if result["reason"] else ")"
    lines = [f"{head}{detail}; {result['metric']} ({result['direction']}); check {result['check']}"]
    lines.append("pooled observed ranges: " + json.dumps(result["observed_ranges"], sort_keys=True))
    lines.extend(f"warning: {w}" for w in result["warnings"])
    return "\n".join(lines)


def _emit(env: _Env, payload: object, text: str, *, as_json: bool) -> None:
    env.out.write((json.dumps(payload, sort_keys=True) if as_json else text) + "\n")


def _cmd_record(args: argparse.Namespace, env: _Env) -> None:
    state = Path(args.dir)
    record = _record_from(args, state, env.host, env.now)
    validate_record(record)
    shards = state / MEASUREMENTS_DIR
    shards.mkdir(exist_ok=True)
    with (shards / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _check_against_history(record, load_records(state))
        with (shards / f"{record['commit']}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=False) + "\n")
    env.out.write(
        f"recorded {record['role']} {record['commit'][:12]} ({len(args.sample)} samples)\n"
    )


def _cmd_compare(args: argparse.Namespace, env: _Env) -> None:
    records = load_records(Path(args.dir))
    result = compare_commit(records, _validate_commit(args.commit), args.base)
    _emit(env, result, _format_comparison(result), as_json=args.json)


def _cmd_best(args: argparse.Namespace, env: _Env) -> None:
    records = load_records(Path(args.dir))
    candidates = list(dict.fromkeys(r["commit"] for r in records if r["role"] == "candidate"))
    won = []
    for commit in candidates:
        result = compare_commit(records, commit, None)
        if result["verdict"] == "better" and result["check"] == "pass":
            won.append(result)
    if not won:
        _emit(
            env,
            {"best": None},
            "no commit has a verdict-backed gain over the base",
            as_json=args.json,
        )
        return
    best = max(won, key=lambda r: r["ratio"])
    payload = {"best": best, "records": [r for r in records if r["commit"] == best["commit"]]}
    _emit(env, payload, _format_comparison(best), as_json=args.json)


_GIT = shutil.which("git") or "git"


def _git(root: Path, *argv: str) -> str:
    done = subprocess.run(  # noqa: S603  # LW-100372; fixed read-only git argv, no shell.
        [_GIT, "-C", str(root), *argv],
        check=False,
        capture_output=True,
        text=True,
    )
    # > A shell would add quoting hazards for branch names; an allowlist wrapper adds
    # > indirection around one fixed command without making the argv safer.
    if done.returncode != 0:
        raise InvalidInputError(f"git {' '.join(argv)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _commit_status(records: list[dict[str, Any]], commit: str) -> dict[str, Any]:
    """One commit's evidence: whether it is recorded, its check and its verdict against base."""
    if not any(r["commit"] == commit for r in records):
        return {"commit": commit, "recorded": False, "check": None, "verdict": None}
    result = compare_commit(records, commit, None)
    return {
        "commit": commit,
        "recorded": True,
        "check": result["check"],
        "verdict": result["verdict"],
    }


def _cmd_check(args: argparse.Namespace, env: _Env) -> int:
    """Read-only: is the branch head proven? Exit 1 when it has no passing ``better`` record."""
    state = Path(args.dir)
    branch = args.branch or f"vibesys/{state.name}"
    root = _repo_root(state)
    records = load_records(state)
    base = next((r["commit"] for r in records if r["role"] == "base"), None)
    if base is None:
        raise InvalidInputError("no base measurement is recorded: measure the base first")
    head = _git(root, "rev-parse", "--verify", f"{branch}^{{commit}}")
    listed = _git(root, "rev-list", "--reverse", f"{base}..{head}").split()
    rows = [_commit_status(records, c) for c in listed]
    if head == base:
        head_row = _commit_status(records, base)
        proven = head_row["check"] == "pass"
    else:
        head_row = rows[-1] if rows else _commit_status(records, head)
        proven = head_row["check"] == "pass" and head_row["verdict"] == "better"
    unrecorded = [r["commit"] for r in rows if not r["recorded"]]
    payload = {
        "branch": branch,
        "base": base,
        "head": head,
        "proven": proven,
        "commits": rows,
        "unrecorded": unrecorded,
    }
    lines = [f"{branch} head {head[:12]} base {base[:12]}: {'proven' if proven else 'NOT proven'}"]
    lines += [
        f"{r['commit'][:12]}  {'recorded' if r['recorded'] else 'NO RECORD':<9}  "
        f"check {r['check'] or '-':<7} verdict {r['verdict'] or '-'}"
        for r in rows
    ]
    if unrecorded:
        lines.append(f"{len(unrecorded)} commit(s) on the branch have no record")
    _emit(env, payload, "\n".join(lines), as_json=args.json)
    return 0 if proven else 1


def _format_record(record: dict[str, Any]) -> str:
    samples = ", ".join(f"{s:g}" for s in record["samples"])
    return (
        f"{record['recorded_at']} {record['role']:<9} {record['commit'][:12]} "
        f"{record['label']}: {record['metric']} ({record['direction']}) "
        f"mean {_mean(record['samples']):g} of [{samples}]; check {record['check'] or 'unknown'}; "
        f"host {record['host']}; exclusive {record['exclusive'] or 'unknown'}"
    )


def _cmd_show(args: argparse.Namespace, env: _Env) -> None:
    records = load_records(Path(args.dir))
    if args.commit:
        records = [r for r in records if r["commit"] == _validate_commit(args.commit)]
    _emit(
        env,
        records,
        "\n".join(_format_record(r) for r in records) or "no records",
        as_json=args.json,
    )


# --- command line ------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InvalidInputError(message)


def _finite_number(text: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from error
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is not finite")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="measurements.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command_name", required=True, parser_class=_Parser)

    runner = commands.add_parser("run", help="execute benchmark load under the host-wide lock")
    runner.add_argument("argv", nargs=argparse.REMAINDER)

    record = commands.add_parser("record", help="append one validated measurement")
    record.add_argument("--dir", required=True)
    record.add_argument("--commit", required=True)
    record.add_argument("--role", required=True, choices=_ROLES)
    record.add_argument("--label", required=True)
    record.add_argument("--metric", required=True)
    record.add_argument("--direction", required=True, choices=_DIRECTIONS)
    record.add_argument("--command", required=True)
    record.add_argument("--sample", required=True, action="append", type=_finite_number)
    record.add_argument(
        "--observations", type=Path, help="JSON mapping auxiliary metric names to numeric samples"
    )
    record.add_argument("--check", choices=_CHECKS)
    record.add_argument("--exclusive", choices=_YES_NO)

    compare = commands.add_parser("compare", help="compare a commit with the base")
    compare.add_argument("--dir", required=True)
    compare.add_argument("--commit", required=True)
    compare.add_argument("--base")
    compare.add_argument("--json", action="store_true")

    best = commands.add_parser("best", help="the best verdict-backed commit")
    best.add_argument("--dir", required=True)
    best.add_argument("--json", action="store_true")

    check = commands.add_parser("check", help="is the branch head proven? (read-only)")
    check.add_argument("--dir", required=True)
    check.add_argument("--branch", help="default: vibesys/<task>")
    check.add_argument("--json", action="store_true")

    show = commands.add_parser("show", help="print the records")
    show.add_argument("--dir", required=True)
    show.add_argument("--commit")
    show.add_argument("--json", action="store_true")
    return parser


_COMMANDS = {
    "record": _cmd_record,
    "compare": _cmd_compare,
    "best": _cmd_best,
    "show": _cmd_show,
    "check": _cmd_check,
}


def _state_directory(value: str) -> Path:
    state = Path(value).resolve()
    if not state.is_dir():
        raise InvalidInputError(f"the state directory {value} does not exist")
    _repo_root(state)
    return state


def _benchmark_run(command: list[str]) -> int:
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise InvalidInputError("run requires -- <benchmark command>")
    # LW-100359 [S108]; one host path must ignore per-shell TMPDIR; random temp
    # files cannot synchronize, and per-repository locks permit cross-run overlap.
    lock_path = Path("/tmp/vibesys-benchmark.lock")  # noqa: S108
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # LW-100354 [S603]; explicit user argv; a shell adds quoting hazards,
        # while an executable allowlist would reject legitimate user benchmarks.
        return subprocess.run(command, check=False, pass_fds=(lock.fileno(),)).returncode  # noqa: S603


def main(
    argv: Sequence[str],
    *,
    now: Callable[[], datetime],
    out: TextIO,
    err: TextIO,
    host: str,
) -> int:
    """Run one subcommand; return the exit code (0 ok, 2 invalid input).

    ``now`` stamps records and ``host`` names the machine, so a caller (a test) fixes both.
    """
    try:
        args = _parser().parse_args(argv)
        if args.command_name == "run":
            return _benchmark_run(args.argv)
        args.dir = str(_state_directory(args.dir))
        return _COMMANDS[args.command_name](args, _Env(out=out, now=now, host=host)) or 0
    except (InvalidInputError, OSError, ValueError) as error:
        err.write(f"error: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1:],
            # LW-100357 [TID251]; standalone host clock; vs_sim adds a dependency, indirect lookup hides real I/O.
            now=lambda: datetime.now(timezone.utc),  # noqa: TID251
            out=sys.stdout,
            err=sys.stderr,
            host=platform.node() or "unknown",
        )
    )
