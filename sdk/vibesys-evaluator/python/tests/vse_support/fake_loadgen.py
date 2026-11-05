"""A Fake of loadgen: the same command line and files, in virtual time, against an in-memory app.

It reads the requests file the package wrote, fills templates from captures,
sends each step to ``app`` (``KvService.handle``'s signature), and writes the
results and summary files the real loadgen writes, with HDR histograms. Time
is virtual: every step takes ``step_micros``; a closed loop's clients take
operations in plan order, an open loop's operations arrive every ``1/rate``.
It is as strict as the schema: it validates every message it reads.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from protobuf import Oneof

from vibesys_evaluator._private.files import DelimitedWriter, SchemaFileError, read_requests
from vibesys_evaluator._private.hdr import Histogram, encode
from vibesys_evaluator._private.loadgen import LoadgenExit
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

T0 = 1_800_000_000 * 10**9
_METHODS = {
    pb.HttpMethod.GET: "GET",
    pb.HttpMethod.POST: "POST",
    pb.HttpMethod.PUT: "PUT",
    pb.HttpMethod.DELETE: "DELETE",
    pb.HttpMethod.PATCH: "PATCH",
}


def _histogram() -> Histogram:
    return Histogram(lowest=1, highest=60_000_000, significant_figures=3)


def _message(h: Histogram) -> pb.HdrHistogram:
    return pb.HdrHistogram(
        lowest_trackable_micros=h.lowest,
        highest_trackable_micros=h.highest,
        significant_figures=h.significant_figures,
        encoded=encode(h),
    )


def _fill(template: pb.Template | None, captured: dict[str, str]) -> str | None:
    if template is None:
        return ""
    out: list[str] = []
    for part in template.parts:
        assert part.part is not None
        if part.part.field == "literal":
            out.append(part.part.value)
            continue
        ref = part.part.value
        if ref.name not in captured:
            return None
        value = captured[ref.name]
        if ref.encoding == pb.RefEncoding.JSON_STRING:
            value = json.dumps(value)[1:-1]
        out.append(value)
    return "".join(out)


def _pointer(body: bytes, pointer: str) -> str | None:
    try:
        value = json.loads(body)
    except ValueError:
        return None
    for escaped in pointer.split("/")[1:]:
        token = escaped.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            return None
    return value if isinstance(value, str) else json.dumps(value)


@dataclass
class FakeLoadgen:
    """Runs phases against ``app``; ``runs`` records each command line."""

    app: Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]
    step_micros: int = 10_000
    #: Per run, a step time that replaces ``step_micros`` (an application that speeds up).
    step_micros_by_run: list[int] = field(default_factory=list)
    cpu_fraction: float = 0.1
    send_lag_micros: int = 0
    #: Open loop: every n-th operation is due but never sent (CANCELLED, start 0); 0: none.
    never_sent_every: int = 0
    runs: list[list[str]] = field(default_factory=list)
    #: The virtual clock: each run starts a second after the previous one ended.
    clock: int = T0

    def run(self, args: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        assert timeout_secs > 0
        self.runs.append(list(args))
        if len(self.runs) <= len(self.step_micros_by_run):
            self.step_micros = self.step_micros_by_run[len(self.runs) - 1]
        parser = argparse.ArgumentParser(exit_on_error=False)
        parser.add_argument("mode", choices=["closed", "open"])
        for name in ("--target", "--requests", "--results", "--summary"):
            parser.add_argument(name, required=True)
        for name in ("--secs", "--warmup", "--rate", "--min-sustained", "--timeout"):
            parser.add_argument(name, type=float, default=0.0)
        parser.add_argument("--clients", type=int, default=0)
        parser.add_argument("--seed", type=int, default=0)
        parser.add_argument("--arrivals", choices=["uniform", "poisson"], default="poisson")
        try:
            options = parser.parse_args(list(args))
        except (argparse.ArgumentError, SystemExit) as error:
            return LoadgenExit(code=2, stderr=f"usage: {error}")
        try:
            header, plan = read_requests(Path(options.requests))
        except SchemaFileError as error:
            return LoadgenExit(code=2, stderr=str(error))
        return self._run(options, header, plan)

    def _execute(
        self, op: pb.PlannedOperation, start: int
    ) -> tuple[list[pb.StepResult], pb.ErrorKind, int]:
        captured: dict[str, str] = {}
        steps: list[pb.StepResult] = []
        now = start
        for step in op.steps:
            assert step.call is not None
            assert step.call.field == "http"
            call = step.call.value
            path = _fill(call.path, captured)
            query = [(p.name, _fill(p.value, captured)) for p in call.query]
            headers = {p.name.lower(): _fill(p.value, captured) for p in call.headers}
            body = b""
            if call.body is not None:
                if call.body.field == "raw":
                    body = call.body.value
                elif call.body.field == "text":
                    body = (_fill(call.body.value, captured) or "").encode()
                else:
                    body = urlencode(
                        [(f.name, _fill(f.value, captured)) for f in call.body.value.fields]
                    ).encode()
            target = path + ("?" + urlencode(query) if query else "") if path is not None else None
            if target is None or any(v is None for v in headers.values()):
                steps.append(
                    pb.StepResult(
                        start_unix_nanos=now,
                        end_unix_nanos=now,
                        error=pb.ErrorKind.CAPTURE,
                        error_message="a ref has no captured value",
                    )
                )
                return steps, pb.ErrorKind.CAPTURE, now
            sent = {name: value for name, value in headers.items() if value is not None}
            status, response = self.app(_METHODS[call.method], target, sent, body)
            end = now + self.step_micros * 1000
            kept = (
                Oneof("body", response)
                if op.keep_bodies
                else Oneof(
                    "digest",
                    pb.BodyDigest(sha256=hashlib.sha256(response).digest(), length=len(response)),
                )
            )
            ok = status in (list(step.expect_status) or [200])
            result = pb.StepResult(
                start_unix_nanos=now,
                end_unix_nanos=end,
                status=status,
                response=kept,
                error=pb.ErrorKind.UNSPECIFIED if ok else pb.ErrorKind.STATUS,
            )
            steps.append(result)
            now = end
            if not ok:
                return steps, pb.ErrorKind.STATUS, now
            for capture in step.captures:
                assert capture.source is not None
                assert capture.source.field == "json_pointer"
                value = _pointer(response, capture.source.value)
                if value is None:
                    result.error = pb.ErrorKind.CAPTURE
                    return steps, pb.ErrorKind.CAPTURE, now
                captured[capture.name] = value
        return steps, pb.ErrorKind.UNSPECIFIED, now

    def _run(
        self,
        options: argparse.Namespace,
        header: pb.RequestsHeader,
        plan: list[pb.PlannedOperation],
    ) -> LoadgenExit:
        closed = options.mode == "closed"
        t0 = self.clock
        window_start = t0 + int(options.warmup * 1e9)
        window_end = window_start + int(options.secs * 1e9)
        results: list[pb.OperationResult] = []
        exhausted = False
        if closed:
            clients = [(t0, n) for n in range(options.clients)]
            heapq.heapify(clients)
            index = 0
            while clients:
                free, client = heapq.heappop(clients)
                if free >= window_end:
                    continue
                if index >= len(plan):
                    exhausted = True
                    break
                steps, error, end = self._execute(plan[index], free)
                results.append(
                    pb.OperationResult(
                        index=index,
                        operation=plan[index].operation,
                        scheduled_unix_nanos=free,
                        start_unix_nanos=free,
                        end_unix_nanos=end,
                        steps=steps,
                        error=error,
                    )
                )
                index += 1
                heapq.heappush(clients, (end, client))
        else:
            gap = 1e9 / options.rate
            exhausted = t0 + int(len(plan) * gap) < window_end
            for index, op in enumerate(plan):
                scheduled = t0 + int(index * gap)
                if scheduled >= window_end:
                    break
                if self.never_sent_every and index % self.never_sent_every == 0:
                    results.append(
                        pb.OperationResult(
                            index=index,
                            operation=op.operation,
                            scheduled_unix_nanos=scheduled,
                            end_unix_nanos=window_end,
                            error=pb.ErrorKind.CANCELLED,
                        )
                    )
                    continue
                start = scheduled + self.send_lag_micros * 1000
                steps, error, end = self._execute(op, start)
                results.append(
                    pb.OperationResult(
                        index=index,
                        operation=op.operation,
                        scheduled_unix_nanos=scheduled,
                        start_unix_nanos=start,
                        end_unix_nanos=end,
                        steps=steps,
                        error=error,
                    )
                )
        self.clock = max([window_end, *(r.end_unix_nanos for r in results)]) + 10**9
        self._write(options, header, results, (window_start, window_end), exhausted=exhausted)
        return LoadgenExit(code=0, stderr="")

    def _write(
        self,
        options: argparse.Namespace,
        header: pb.RequestsHeader,
        results: list[pb.OperationResult],
        window: tuple[int, int],
        *,
        exhausted: bool,
    ) -> None:
        window_start, window_end = window
        closed = options.mode == "closed"
        with Path(options.results).open("wb") as file:
            writer = DelimitedWriter(file)
            writer.write_header(pb.FileKind.RESULTS)
            for result in sorted(results, key=lambda r: (r.end_unix_nanos, r.index)):
                writer.write(result)
        latency, lag = _histogram(), _histogram()
        per_op = {n: (_histogram(), [0, 0]) for n in range(len(header.operation_names))}
        sent = completed = errors = spilled = in_time = 0
        by_kind: dict[int, int] = {}
        # As loadgen counts (internal/summary): an operation is the window's when it was
        # scheduled in it; latency is end - scheduled, of successful operations only.
        for result in results:
            if window_start <= result.start_unix_nanos < window_end:
                sent += 1
            if not window_start <= result.scheduled_unix_nanos < window_end:
                continue
            completed += 1
            lag.record((result.start_unix_nanos - result.scheduled_unix_nanos) // 1000)
            histogram, counts = per_op[result.operation]
            counts[0] += 1
            if result.error != pb.ErrorKind.UNSPECIFIED:
                errors += 1
                counts[1] += 1
                by_kind[int(result.error)] = by_kind.get(int(result.error), 0) + 1
            else:
                micros = (result.end_unix_nanos - result.scheduled_unix_nanos) // 1000
                latency.record(micros)
                histogram.record(micros)
            if result.end_unix_nanos > window_end:
                spilled += 1
            else:
                in_time += 1
        secs = (window_end - window_start) / 1e9
        offered = sent / secs
        summary = pb.PhaseSummary(
            model=pb.LoadModel.CLOSED if closed else pb.LoadModel.OPEN,
            target_rate=0.0 if closed else options.rate,
            clients=options.clients if closed else 0,
            window_start_unix_nanos=window_start,
            window_end_unix_nanos=window_end,
            sent=sent,
            completed=completed,
            errors=errors,
            errors_by_kind=by_kind,
            spilled=spilled,
            offered_rate=offered,
            achieved_rate=in_time / secs,
            send_lag=_message(lag),
            max_send_lag_micros=self.send_lag_micros,
            loadgen_cpu_fraction=self.cpu_fraction,
            sustained=closed or offered >= options.min_sustained * options.rate,
            exhausted=exhausted,
            latency=_message(latency),
            operations=[
                pb.OperationSummary(operation=n, completed=c[0], errors=c[1], latency=_message(h))
                for n, (h, c) in per_op.items()
            ],
        )
        with Path(options.summary).open("wb") as file:
            writer = DelimitedWriter(file)
            writer.write_header(pb.FileKind.SUMMARY)
            writer.write(summary)
