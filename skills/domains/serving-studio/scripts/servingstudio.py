#!/usr/bin/env python3
"""Talk to a ServingStudio public API and keep what a prediction needs to be traced later.

Commands (the endpoint is ``SERVINGSTUDIO_API``, else the hosted service):

    servingstudio.py health
        Print the endpoint and the simulator revision it serves, as one JSON line.
    servingstudio.py simulate --request REQUEST.json --out DIR
        Queue the simulation REQUEST.json describes (the body of ``POST /simulate``), wait for it
        within the service's own limits, and write DIR/request.json, DIR/record.json (the final
        ``GET /simulations/{id}``) and DIR/provenance.json (endpoint, simulator revision before
        and after the run, simulation id, times, status). Both files are written as soon as the
        service accepts the run and again after every poll, so a crash keeps the simulation id.
        Print the provenance as one JSON line.
    servingstudio.py record --kind calibration|study --file RECORD.json [--dir DIR]
        Check RECORD.json against the format of its kind (references/calibration.md,
        references/design-study.md) and keep it as DIR/<kind>-NNN.json, the next free number,
        never replacing a record. DIR is ``SERVINGSTUDIO_RECORDS`` unless given. A calibration
        must name the latest calibration in DIR as the one it supersedes, or none when it is
        the first. Print the new record's id and path as one JSON line.

Exit status: 0 the command answered (``simulate``: the run is ``done`` and every request
finished; ``record``: the record is kept); 1 the run ended ``failed`` or ``timed_out``, did not end before the deadline, or left
requests unfinished; 2 the request was refused (DIR/submission-error.json keeps the answer), the
endpoint stopped answering, or answered something this script does not understand; 3 the run is
done but the service's revision changed or could not be read around it; 64 usage, or a record
that does not meet its format (the message names the key).

Only the Python standard library is used, so any Python 3.10 or newer runs it. Every network
call has a timeout, every retry is bounded, and the overall wait is bounded by the service's
10-minute run limit plus a margin.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_API = "https://servingstudio.cs.washington.edu/api/public/v1"
"""The hosted service, the same default the upstream ServingStudio skills use."""

TERMINAL = frozenset({"done", "failed", "timed_out"})
POLL_SECONDS = 5.0
WAIT_LIMIT_SECONDS = 13 * 60.0
"""The service stops a run at 10 minutes; a queue wait and a margin come on top."""
SUBMIT_ATTEMPTS = 6
MAX_RETRY_AFTER_SECONDS = 60.0
POLL_FAILURES_TOLERATED = 3
REQUEST_TIMEOUT_SECONDS = 30.0
EXIT_OK, EXIT_RUN_FAILED, EXIT_REFUSED, EXIT_REVISION_UNCERTAIN, EXIT_USAGE = 0, 1, 2, 3, 64


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    """One HTTP exchange. Raises ``OSError`` when the endpoint cannot be reached."""

    def exchange(self, method: str, url: str, body: bytes | None) -> Response: ...


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class UrllibTransport:
    def exchange(self, method: str, url: str, body: bytes | None) -> Response:
        headers = {"content-type": "application/json"} if body is not None else {}
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
                return Response(reply.status, dict(reply.headers.items()), reply.read())
        except urllib.error.HTTPError as refused:
            return Response(refused.code, dict(refused.headers.items()), refused.read())


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class UsageError(Exception):
    """The command line asks for something this script refuses to do."""


class ApiError(Exception):
    """The endpoint refused a request, did not answer, or answered something unexpected."""

    def __init__(self, message: str, response: Response | None = None) -> None:
        super().__init__(message)
        self.response = response


def _iso(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).isoformat()


def _json(response: Response, what: str) -> dict[str, object]:
    try:
        value = json.loads(response.body)
    except ValueError as error:
        raise ApiError(f"{what}: HTTP {response.status}, not JSON: {error}") from error
    if not isinstance(value, dict):
        raise ApiError(f"{what}: HTTP {response.status}, expected a JSON object")
    return value


def _call(transport: Transport, method: str, url: str, body: bytes | None = None) -> Response:
    try:
        return transport.exchange(method, url, body)
    except OSError as error:
        raise ApiError(f"{method} {url}: no answer: {error}") from error


def health(transport: Transport, api: str) -> dict[str, object]:
    """The endpoint and the simulator revision it serves; raise ``ApiError`` unless healthy."""
    response = _call(transport, "GET", f"{api}/health")
    answer = _json(response, "GET /health")
    if response.status != 200 or answer.get("status") != "ok":
        raise ApiError(f"GET {api}/health: HTTP {response.status}: {answer}")
    commit = answer.get("sim_commit")
    if not isinstance(commit, str) or not commit:
        raise ApiError(f"GET {api}/health: names no simulator revision (sim_commit): {answer}")
    return {"endpoint": api, "sim_commit": commit}


def _retry_after(response: Response) -> float:
    for key, value in response.headers.items():
        if key.lower() == "retry-after":
            try:
                return min(MAX_RETRY_AFTER_SECONDS, max(1.0, float(value)))
            except ValueError:
                break
    return 10.0


def _submit(transport: Transport, clock: Clock, api: str, body: bytes) -> dict[str, object]:
    for _ in range(SUBMIT_ATTEMPTS):
        response = _call(transport, "POST", f"{api}/simulate", body)
        if response.status == 429:
            clock.sleep(_retry_after(response))
            continue
        answer = _json(response, "POST /simulate")
        if response.status not in (200, 202) or not isinstance(answer.get("simulation_id"), str):
            raise ApiError(f"POST {api}/simulate: HTTP {response.status}: {answer}", response)
        return answer
    raise ApiError(f"POST {api}/simulate: still rate limited after {SUBMIT_ATTEMPTS} attempts")


def simulate(
    transport: Transport, clock: Clock, api: str, request: dict[str, object], out: Path
) -> tuple[int, dict[str, object]]:
    """Run one simulation to its end and write it with its provenance; return exit status."""
    if out.joinpath("provenance.json").exists() or out.joinpath("submission-error.json").exists():
        raise UsageError(f"{out} already holds a simulation; give each run its own --out")
    revision = health(transport, api)
    out.mkdir(parents=True, exist_ok=True)
    out.joinpath("request.json").write_text(json.dumps(request, indent=1) + "\n")
    submitted_at = clock.now()
    try:
        record: dict[str, object] = _submit(transport, clock, api, json.dumps(request).encode())
    except ApiError as refused:
        answer = refused.response
        error = {
            **revision,
            "at": _iso(clock.now()),
            "error": str(refused),
            "http_status": answer.status if answer else None,
            "body": answer.body.decode(errors="replace") if answer else None,
        }
        out.joinpath("submission-error.json").write_text(json.dumps(error, indent=1) + "\n")
        raise
    simulation_id = str(record["simulation_id"])
    provenance: dict[str, object] = {
        **revision,
        "simulation_id": simulation_id,
        "preset": request.get("preset"),
        "params": request.get("params"),
        "submitted_at": _iso(submitted_at),
    }

    def keep(status: object) -> None:
        provenance.update(ended_at=_iso(clock.now()), status=status, routing=record.get("routing"))
        out.joinpath("record.json").write_text(json.dumps(record, indent=1) + "\n")
        out.joinpath("provenance.json").write_text(json.dumps(provenance, indent=1) + "\n")

    keep(record.get("status"))
    failures = 0
    while record.get("status") not in TERMINAL and clock.now() - submitted_at < WAIT_LIMIT_SECONDS:
        clock.sleep(POLL_SECONDS)
        try:
            response = _call(transport, "GET", f"{api}/simulations/{simulation_id}")
            if response.status != 200:
                raise ApiError(f"GET {api}/simulations/{simulation_id}: HTTP {response.status}")
            record = _json(response, "GET /simulations/{id}")
        except ApiError:
            failures += 1
            if failures > POLL_FAILURES_TOLERATED:
                keep("lost_contact")
                raise
            continue
        failures = 0
        keep(record.get("status"))
    status = record.get("status")
    try:
        after = health(transport, api)["sim_commit"]
    except ApiError:
        after = None
    provenance["sim_commit_after"] = after
    provenance["revision_verified"] = after == revision["sim_commit"]
    requests = _summary_requests(record)
    complete = status == "done" and requests is not None and requests[0] == requests[1]
    provenance["complete"] = complete
    if status == "done" and not complete:
        status = "incomplete"
    keep(status if status in (*TERMINAL, "incomplete") else "gave_up_waiting")
    if not complete:
        return EXIT_RUN_FAILED, provenance
    if not provenance["revision_verified"]:
        print(
            f"servingstudio.py: the service's revision was {revision['sim_commit']} before the run "
            f"and {after} after it; this result cannot be frozen as either",
            file=sys.stderr,
        )
        return EXIT_REVISION_UNCERTAIN, provenance
    return EXIT_OK, provenance


def _summary_requests(record: dict[str, object]) -> tuple[object, object] | None:
    """(finished, total) from a done record's summary, or None when it does not say."""
    summary = record.get("summary")
    if not isinstance(summary, dict):
        return None
    requests = summary.get("requests")
    if not isinstance(requests, dict) or "total" not in requests or "finished" not in requests:
        return None
    return requests["finished"], requests["total"]


RECORD_KINDS = ("calibration", "study")
# What a calibration was fitted on: an official measure of the head (the default when absent),
# or a timed run of the head's engine on the device (a command job): a provisional record.
FITTED_SOURCES = ("measure", "timed-run")
RECORD_ATTEMPTS = 20
_HEX = frozenset("0123456789abcdef")


class RecordError(Exception):
    """A record does not meet the format of its kind; the message names the key."""


def _fields(
    value: object, where: str, required: set[str], optional: frozenset[str] = frozenset()
) -> dict:
    if not isinstance(value, dict):
        raise RecordError(f"{where}: expected an object")
    missing = sorted(required - value.keys())
    if missing:
        raise RecordError(f"{where}: missing {', '.join(missing)}")
    unknown = sorted(value.keys() - required - optional)
    if unknown:
        raise RecordError(f"{where}: unknown key {', '.join(unknown)}")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecordError(f"{where}: expected a non-empty string")
    return value


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError(f"{where}: expected a number")
    return float(value)


def _revision(value: object, where: str) -> str:
    if not isinstance(value, str) or not 7 <= len(value) <= 64 or not set(value) <= _HEX:
        raise RecordError(f"{where}: expected a lower-case hexadecimal commit, 7 to 64 characters")
    return value


def _items(value: object, where: str) -> list:
    if not isinstance(value, list) or not value:
        raise RecordError(f"{where}: expected a non-empty list")
    return value


def _overrides(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise RecordError(f"{where}: expected an object of override paths")
    for path in value:
        _text(path, f"{where} key")
    return value


def _simulator(value: object) -> None:
    sim = _fields(value, "simulator", {"checkout", "revision"}, frozenset({"pinned_by"}))
    _text(sim["checkout"], "simulator.checkout")
    _revision(sim["revision"], "simulator.revision")
    if "pinned_by" in sim and sim["pinned_by"] not in ("vibesys.input.toml", "pins.toml"):
        raise RecordError("simulator.pinned_by: expected vibesys.input.toml or pins.toml")


def check_calibration(record: object) -> dict:
    """Raise ``RecordError`` unless ``record`` is a calibration record; return it."""
    r = _fields(
        record,
        "calibration",
        {
            "simulator", "preset", "workload", "fitted_on", "factors", "residual_before",
            "residual_after", "engine_limitations_not_fitted", "commands", "result_dirs",
            "supersedes",
        },
    )  # fmt: skip
    _simulator(r["simulator"])
    _text(r["preset"], "preset")
    _text(r["workload"], "workload")
    metrics = set()
    for i, m in enumerate(_items(r["fitted_on"], "fitted_on")):
        where = f"fitted_on[{i}]"
        required = {"commit", "job", "metric", "measured", "timeline"}
        _fields(m, where, required, frozenset({"source"}))
        if m.get("source", "measure") not in FITTED_SOURCES:
            raise RecordError(f"{where}.source: expected one of {', '.join(FITTED_SOURCES)}")
        _revision(m["commit"], f"{where}.commit")
        _text(m["job"], f"{where}.job")
        metrics.add(_text(m["metric"], f"{where}.metric"))
        _number(m["measured"], f"{where}.measured")
        _text(m["timeline"], f"{where}.timeline")
    factors = r["factors"]
    if not isinstance(factors, dict) or not factors:
        raise RecordError("factors: expected a non-empty object of override paths")
    for path, factor in factors.items():
        if _number(factor, f"factors.{path}") <= 0:
            raise RecordError(f"factors.{path}: expected a positive number")
    for key in ("residual_before", "residual_after"):
        residuals = r[key]
        if not isinstance(residuals, dict):
            raise RecordError(f"{key}: expected an object of metric to signed residual")
        absent = sorted(metrics - residuals.keys())
        if absent:
            raise RecordError(f"{key}: no residual for {', '.join(absent)}")
        for metric, residual in residuals.items():
            _number(residual, f"{key}.{metric}")
    limits = r["engine_limitations_not_fitted"]
    if not isinstance(limits, list):
        raise RecordError("engine_limitations_not_fitted: expected a list, empty when none")
    for i, limit in enumerate(limits):
        _text(limit, f"engine_limitations_not_fitted[{i}]")
    for key in ("commands", "result_dirs"):
        for i, item in enumerate(_items(r[key], key)):
            _text(item, f"{key}[{i}]")
    if r["supersedes"] is not None:
        _text(r["supersedes"], "supersedes")
    return r


def check_study(record: object) -> dict:
    """Raise ``RecordError`` unless ``record`` is a design-study record; return it."""
    r = _fields(
        record,
        "study",
        {"simulator", "preset", "workload", "metric", "calibration", "axes", "scenarios",
         "ladder", "best"},
    )  # fmt: skip
    _simulator(r["simulator"])
    _text(r["preset"], "preset")
    _text(r["workload"], "workload")
    _text(r["metric"], "metric")
    if r["calibration"] is not None:
        _text(r["calibration"], "calibration")
    swept = 0
    for i, axis in enumerate(_items(r["axes"], "axes")):
        where = f"axes[{i}]"
        _fields(axis, where, {"name", "values", "assumption"})
        name = _text(axis["name"], f"{where}.name")
        values = _items(axis["values"], f"{where}.values")
        if axis["assumption"] is None:
            swept += len(values) > 1
        else:
            _text(axis["assumption"], f"{where}.assumption")
            if len(values) != 1:
                raise RecordError(f"{where}: {name} is held as an assumption, so it has one value")
    if not swept:
        raise RecordError("axes: no axis is swept (every axis has one value or an assumption)")
    scenarios = _items(r["scenarios"], "scenarios")
    if len(scenarios) < 2:
        raise RecordError("scenarios: expected today's kernels and at least one other scenario")
    for i, scenario in enumerate(scenarios):
        _fields(scenario, f"scenarios[{i}]", {"name", "overrides"})
        _text(scenario["name"], f"scenarios[{i}].name")
        _overrides(scenario["overrides"], f"scenarios[{i}].overrides")
    if not any(not s["overrides"] for s in scenarios):
        raise RecordError("scenarios: none is today's kernels (a scenario with no overrides)")
    previous: dict = {}
    for i, step in enumerate(_items(r["ladder"], "ladder")):
        where = f"ladder[{i}]"
        _fields(step, where, {"name", "overrides", "values", "command", "result_dir"})
        _text(step["name"], f"{where}.name")
        overrides = _overrides(step["overrides"], f"{where}.overrides")
        if i == 0 and overrides:
            raise RecordError("ladder[0]: the first step is today's engine, with no overrides")
        dropped = sorted(previous.keys() - overrides.keys())
        if dropped:
            raise RecordError(f"{where}: not cumulative, drops {', '.join(dropped)}")
        previous = overrides
        _concurrency_values(step["values"], f"{where}.values")
        _text(step["command"], f"{where}.command")
        _text(step["result_dir"], f"{where}.result_dir")
    seen = set()
    for i, best in enumerate(_items(r["best"], "best")):
        where = f"best[{i}]"
        _fields(best, where, {"concurrency", "overrides", "value", "command", "result_dir"})
        c = best["concurrency"]
        if isinstance(c, bool) or not isinstance(c, int) or c < 1:
            raise RecordError(f"{where}.concurrency: expected a positive integer")
        if c in seen:
            raise RecordError(f"{where}.concurrency: {c} is listed twice")
        seen.add(c)
        _overrides(best["overrides"], f"{where}.overrides")
        _number(best["value"], f"{where}.value")
        _text(best["command"], f"{where}.command")
        _text(best["result_dir"], f"{where}.result_dir")
    return r


def _concurrency_values(value: object, where: str) -> None:
    if not isinstance(value, dict) or not value:
        raise RecordError(f"{where}: expected an object of concurrency to metric value")
    for c, v in value.items():
        if not c.isdigit() or int(c) < 1:
            raise RecordError(f"{where}: {c!r} is not a concurrency (a positive integer)")
        _number(v, f"{where}.{c}")


def _record_ids(directory: Path, kind: str) -> list[int]:
    numbers = []
    for path in directory.glob(f"{kind}-*.json"):
        suffix = path.stem[len(kind) + 1 :]
        if suffix.isdigit():
            numbers.append(int(suffix))
    return sorted(numbers)


def keep_record(directory: Path, kind: str, record: object) -> dict[str, str]:
    """Check ``record`` and write it as the next ``<kind>-NNN.json`` in ``directory``."""
    if kind == "calibration":
        checked = check_calibration(record)
        ids = _record_ids(directory, kind) if directory.is_dir() else []
        latest = f"{kind}-{ids[-1]:03d}" if ids else None
        if checked["supersedes"] != latest:
            raise RecordError(
                f"supersedes: the latest calibration in {directory} is {latest}, "
                f"not {checked['supersedes']}"
            )
    else:
        check_study(record)
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=1) + "\n"
    for _ in range(RECORD_ATTEMPTS):
        ids = _record_ids(directory, kind)
        record_id = f"{kind}-{(ids[-1] if ids else 0) + 1:03d}"
        path = directory / f"{record_id}.json"
        try:
            with path.open("x") as out:  # exclusive: a record is never replaced
                out.write(text)
        except FileExistsError:
            continue  # another agent took this number; take the next
        return {"id": record_id, "path": str(path)}
    raise RecordError(f"{directory}: no free {kind} number after {RECORD_ATTEMPTS} attempts")


def _record(kind: str, file: Path, directory: Path | str | None) -> int:
    if not directory:
        print("servingstudio.py: give --dir or set SERVINGSTUDIO_RECORDS", file=sys.stderr)
        return EXIT_USAGE
    try:
        record = json.loads(file.read_text())
    except (OSError, ValueError) as error:
        print(f"servingstudio.py: cannot read {file}: {error}", file=sys.stderr)
        return EXIT_USAGE
    try:
        kept = keep_record(Path(directory), kind, record)
    except RecordError as error:
        print(f"servingstudio.py: {file}: {error}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(kept))
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    clock: Clock | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="servingstudio.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="print the endpoint and its simulator revision")
    run = commands.add_parser("simulate", help="run one simulation and keep its provenance")
    run.add_argument("--request", type=Path, required=True, help="body of POST /simulate")
    run.add_argument("--out", type=Path, required=True, help="directory for the result")
    keep = commands.add_parser("record", help="check a calibration or study record and keep it")
    keep.add_argument("--kind", choices=RECORD_KINDS, required=True)
    keep.add_argument("--file", type=Path, required=True, help="the record, as JSON")
    keep.add_argument("--dir", type=Path, help="records directory (SERVINGSTUDIO_RECORDS)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return EXIT_USAGE if stop.code else EXIT_OK
    env = os.environ if environ is None else environ
    if args.command == "record":
        return _record(args.kind, args.file, args.dir or env.get("SERVINGSTUDIO_RECORDS"))
    api = (env.get("SERVINGSTUDIO_API") or DEFAULT_API).rstrip("/")
    transport = transport or UrllibTransport()
    clock = clock or SystemClock()
    try:
        if args.command == "health":
            print(json.dumps(health(transport, api)))
            return EXIT_OK
        try:
            request = json.loads(args.request.read_text())
        except (OSError, ValueError) as error:
            print(f"servingstudio.py: cannot read {args.request}: {error}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(request, dict):
            print(f"servingstudio.py: {args.request} must hold a JSON object", file=sys.stderr)
            return EXIT_USAGE
        status, provenance = simulate(transport, clock, api, request, args.out)
        print(json.dumps(provenance))
        return status
    except UsageError as error:
        print(f"servingstudio.py: {error}", file=sys.stderr)
        return EXIT_USAGE
    except ApiError as error:
        print(f"servingstudio.py: {error}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
