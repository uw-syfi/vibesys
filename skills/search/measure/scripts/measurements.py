#!/usr/bin/env python3
"""Record and compare benchmark measurements of commits for one optimization run.

The single writer of a run's ``measurements.jsonl``. It uses only the Python standard
library (3.10 or newer) and runs from an installed Claude Code plugin, outside any VibeSys
package, so nothing here may import one.

Subcommands (every one takes ``--run DIR``, the run's directory ``.vibesys-optimize/<run-id>``):

* ``record``: validate one measurement and append it.
* ``compare``: a commit's samples against the base's.
* ``best``: the commit with the best verdict-backed result.
* ``show``: the records.

``--json`` makes ``compare``, ``best`` and ``show`` print JSON. Exit codes: 0 ok, 2 invalid input.

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
    "gate_summary",
    "load_records",
    "main",
    "objective_gates",
    "task_fingerprint",
]

SCHEMA_VERSION = 1
MEASUREMENTS_FILE = "measurements.jsonl"
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
        and not any(
            part in _SKIPPED_PARTS or part == ".env" or part.startswith(".env.") for part in p.parts
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
    unknown = sorted(set(record) - set(_FIELDS) - {"gate_requirements", "gates"})
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
    _validate_gates(record)
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


def _validate_gates(record: dict[str, Any]) -> None:
    requirements = record.get("gate_requirements", [])
    gates = record.get("gates", {})
    if not isinstance(requirements, list) or not all(isinstance(g, str) for g in requirements):
        raise InvalidInputError("gate_requirements must be a list of names")
    if not isinstance(gates, dict) or set(gates) - set(requirements):
        raise InvalidInputError("gates must name declared objective gates")
    for name, gate in gates.items():
        if not isinstance(gate, dict) or set(gate) != {"passed", "values"}:
            raise InvalidInputError(f"gate {name}: expected passed and values")
        if not isinstance(gate["passed"], list) or any(type(v) is not bool for v in gate["passed"]):
            raise InvalidInputError(f"gate {name}: passed must contain booleans")
        _validate_samples(gate["values"])
        if len(gate["passed"]) != len(record["samples"]) or len(gate["values"]) != len(
            record["samples"]
        ):
            raise InvalidInputError(f"gate {name}: one result and value per sample required")


def load_records(run: Path) -> list[dict[str, Any]]:
    """Read validated records; malformed lines name their file and line."""
    path = run / MEASUREMENTS_FILE
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(validate_record(json.loads(line)))
        except (ValueError, InvalidInputError) as error:
            raise InvalidInputError(f"{path} line {number}: {error}") from error
    return records


def _check_against_history(new: dict[str, Any], history: list[dict[str, Any]]) -> None:
    bases = {r["commit"] for r in history if r["role"] == "base"}
    if new["role"] == "base" and bases and new["commit"] not in bases:
        raise InvalidInputError(
            f"the run's base is {sorted(bases)[0]}; a second base commit is not allowed"
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


def _task_dir(run: Path) -> Path:
    meta = run / "run.json"
    if not meta.is_file():
        raise InvalidInputError(f"{meta} does not exist: this is not a run directory")
    try:
        task = json.loads(meta.read_text(encoding="utf-8"))["task"]
    except (ValueError, KeyError, TypeError) as error:
        raise InvalidInputError(f'{meta} has no readable "task"') from error
    directory = run.parent.parent / ".vibesys" / "tasks" / str(task)
    if not directory.is_dir():
        raise InvalidInputError(f"the task directory {directory} does not exist")
    return directory


def objective_gates(task: Path) -> list[str]:
    """Conservatively require evidence for every gate/SLA declaration in task Markdown.

    Names are the exact stripped declaration lines, so no domain vocabulary or numeric
    threshold is guessed. The measurer interprets each rule and records per-sample results.
    """
    return sorted(
        {
            line.strip()
            for path in task.glob("*.md")
            for line in path.read_text().splitlines()
            if re.search(r"\b(?:gates?|sla)\b", line, re.IGNORECASE)
        }
    )


def gate_summary(
    records: list[dict[str, Any]], required: list[str] | None = None
) -> dict[str, Any]:
    """Pool evidence fail-closed; a failed sample always dominates missing data."""
    names = set(required or []) | {g for r in records for g in r.get("gate_requirements", [])}
    failed = any(not all(g["passed"]) for r in records for g in r.get("gates", {}).values())
    missing = any(
        not records or any(name not in r.get("gates", {}) for r in records) for name in names
    )
    return {
        "status": "fail" if failed else "unmeasured" if missing else "pass",
        "ranges": {
            name: [min(values), max(values)]
            for name in sorted(names)
            if (
                values := [
                    v for r in records for v in r.get("gates", {}).get(name, {}).get("values", [])
                ]
            )
        },
    }


def _record_from(
    args: argparse.Namespace, run: Path, host: str, now: Callable[[], datetime]
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
        "evaluator": task_fingerprint(_task_dir(run)),
        "gate_requirements": objective_gates(_task_dir(run)),
        "gates": json.loads(args.gates.read_text()) if args.gates else {},
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
        "gates": {"candidate": gate_summary(mine), "base": gate_summary(theirs)},
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
    lines.append(
        "objective gates and pooled ranges: " + json.dumps(result["gates"], sort_keys=True)
    )
    lines.extend(f"warning: {w}" for w in result["warnings"])
    return "\n".join(lines)


def _emit(env: _Env, payload: object, text: str, *, as_json: bool) -> None:
    env.out.write((json.dumps(payload, sort_keys=True) if as_json else text) + "\n")


def _cmd_gates(args: argparse.Namespace, env: _Env) -> None:
    env.out.write(json.dumps(objective_gates(_task_dir(Path(args.run))), indent=2) + "\n")


def _cmd_record(args: argparse.Namespace, env: _Env) -> None:
    run = Path(args.run)
    record = _record_from(args, run, env.host, env.now)
    validate_record(record)
    meta = json.loads((run / "run.json").read_text())
    if args.role == "base" and meta.get("base_commit", args.commit) != args.commit:
        raise InvalidInputError("--role base must match run.json base_commit")
    _check_against_history(record, load_records(run))
    with (run / MEASUREMENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=False) + "\n")
    env.out.write(
        f"recorded {record['role']} {record['commit'][:12]} ({len(args.sample)} samples)\n"
    )


def _cmd_compare(args: argparse.Namespace, env: _Env) -> None:
    records = load_records(Path(args.run))
    result = compare_commit(records, _validate_commit(args.commit), args.base)
    required = objective_gates(_task_dir(Path(args.run)))
    result["gates"] = {
        "candidate": gate_summary(
            [r for r in records if r["commit"] == result["commit"]], required
        ),
        "base": gate_summary([r for r in records if r["commit"] == result["base"]], required),
    }
    _emit(env, result, _format_comparison(result), as_json=args.json)


def _cmd_best(args: argparse.Namespace, env: _Env) -> None:
    records = load_records(Path(args.run))
    required = objective_gates(_task_dir(Path(args.run)))
    candidates = list(dict.fromkeys(r["commit"] for r in records if r["role"] == "candidate"))
    won = []
    for commit in candidates:
        result = compare_commit(records, commit, None)
        result["gates"]["candidate"] = gate_summary(
            [r for r in records if r["commit"] == commit], required
        )
        if (
            result["verdict"] == "better"
            and result["check"] == "pass"
            and result["gates"]["candidate"]["status"] == "pass"
        ):
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


def _format_record(record: dict[str, Any]) -> str:
    samples = ", ".join(f"{s:g}" for s in record["samples"])
    return (
        f"{record['recorded_at']} {record['role']:<9} {record['commit'][:12]} "
        f"{record['label']}: {record['metric']} ({record['direction']}) "
        f"mean {_mean(record['samples']):g} of [{samples}]; check {record['check'] or 'unknown'}; "
        f"host {record['host']}; exclusive {record['exclusive'] or 'unknown'}"
    )


def _cmd_show(args: argparse.Namespace, env: _Env) -> None:
    records = load_records(Path(args.run))
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

    gates = commands.add_parser("gates", help="list exact task objective gate declaration keys")
    gates.add_argument("--run", required=True)

    record = commands.add_parser("record", help="append one validated measurement")
    record.add_argument("--run", required=True)
    record.add_argument("--commit", required=True)
    record.add_argument("--role", required=True, choices=_ROLES)
    record.add_argument("--label", required=True)
    record.add_argument("--metric", required=True)
    record.add_argument("--direction", required=True, choices=_DIRECTIONS)
    record.add_argument("--command", required=True)
    record.add_argument("--sample", required=True, action="append", type=_finite_number)
    record.add_argument(
        "--gates", type=Path, help="JSON objective gate results, one value and pass per sample"
    )
    record.add_argument("--check", choices=_CHECKS)
    record.add_argument("--exclusive", choices=_YES_NO)

    compare = commands.add_parser("compare", help="compare a commit with the base")
    compare.add_argument("--run", required=True)
    compare.add_argument("--commit", required=True)
    compare.add_argument("--base")
    compare.add_argument("--json", action="store_true")

    best = commands.add_parser("best", help="the best verdict-backed commit")
    best.add_argument("--run", required=True)
    best.add_argument("--json", action="store_true")

    show = commands.add_parser("show", help="print the records")
    show.add_argument("--run", required=True)
    show.add_argument("--commit")
    show.add_argument("--json", action="store_true")
    return parser


_COMMANDS = {
    "record": _cmd_record,
    "gates": _cmd_gates,
    "compare": _cmd_compare,
    "best": _cmd_best,
    "show": _cmd_show,
}


def _run_directory(value: str) -> Path:
    run = Path(value).resolve()
    if not run.is_dir():
        raise InvalidInputError(f"the run directory {value} does not exist")
    return run


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
        return subprocess.run(command, check=False).returncode  # noqa: S603


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
        run = _run_directory(args.run)
        args.run = str(run)
        if args.command_name == "record":
            with (run / "run.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                _COMMANDS[args.command_name](args, _Env(out=out, now=now, host=host))
        else:
            _COMMANDS[args.command_name](args, _Env(out=out, now=now, host=host))
    except InvalidInputError as error:
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
