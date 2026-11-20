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

Exit status: 0 the command answered (``simulate``: the run is ``done`` and every request
finished); 1 the run ended ``failed`` or ``timed_out``, did not end before the deadline, or left
requests unfinished; 2 the request was refused (DIR/submission-error.json keeps the answer), the
endpoint stopped answering, or answered something this script does not understand; 3 the run is
done but the service's revision changed or could not be read around it; 64 usage.

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
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return EXIT_USAGE if stop.code else EXIT_OK
    env = os.environ if environ is None else environ
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
