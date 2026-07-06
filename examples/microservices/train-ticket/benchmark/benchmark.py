#!/usr/bin/env python3
"""Read-only HTTP benchmark for the Train Ticket microservice app."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    path: str
    weight: int = 1
    body: Any | None = None
    expect_text: str | None = None
    expect_json: bool = True


ENDPOINTS = [
    Endpoint("stations", "GET", "/api/v1/stationservice/stations", weight=3),
    Endpoint("trains", "GET", "/api/v1/trainservice/trains", weight=3),
    Endpoint("trips", "GET", "/api/v1/travelservice/trips", weight=4),
    Endpoint("routes", "GET", "/api/v1/routeservice/routes", weight=2),
    Endpoint("prices", "GET", "/api/v1/priceservice/prices", weight=2),
    Endpoint("configs", "GET", "/api/v1/configservice/configs", weight=1),
    Endpoint(
        "travel welcome",
        "GET",
        "/api/v1/travelservice/welcome",
        weight=1,
        expect_text="Welcome to [ Travel Service ]",
        expect_json=False,
    ),
]

DIRECT_SERVICE_PORTS = {
    "configservice": 15679,
    "stationservice": 12345,
    "trainservice": 14567,
    "travelservice": 12346,
    "routeservice": 11178,
    "priceservice": 16579,
}


def normalize_base_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.lower().startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/") + "/"


def format_host(hostname: str) -> str:
    # urlsplit().hostname strips brackets from IPv6 literals; restore them.
    if ":" in hostname:
        return f"[{hostname}]"
    return hostname


def endpoint_url(base_url: str, endpoint: Endpoint, direct_services: bool) -> str:
    if not direct_services:
        return urljoin(base_url, endpoint.path.lstrip("/"))
    parts = endpoint.path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "api" or parts[1] != "v1":
        raise RuntimeError(
            f"{endpoint.name}: cannot map path to direct service URL: {endpoint.path}"
        )
    service = parts[2]
    port = DIRECT_SERVICE_PORTS.get(service)
    if port is None:
        raise RuntimeError(f"{endpoint.name}: no direct port mapping for service {service!r}")
    base = urlsplit(base_url)
    host = format_host(base.hostname or "localhost")
    return urlunsplit((base.scheme or "http", f"{host}:{port}", endpoint.path, "", ""))


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile (same convention as numpy's default)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def request_once(
    base_url: str,
    endpoint: Endpoint,
    timeout: float,
    direct_services: bool,
    scheduled_start: float,
) -> dict[str, Any]:
    """Issue one request and record it; never raises.

    Timing model (open loop): `latency_ms` runs from the *scheduled* send time,
    so time spent waiting in the client-side queue when the pool is saturated
    counts against the deployment, exactly as a real arrival at that instant
    would experience it. `service_time_ms` covers only the HTTP exchange, and
    `queue_wait_ms` is the difference between the two.
    """
    dequeued = time.perf_counter()
    status = 0
    body = b""
    error = None
    try:
        url = endpoint_url(base_url, endpoint, direct_services)
        headers = {"Accept": "application/json,text/plain,*/*"}
        data = None
        if endpoint.body is not None:
            data = json.dumps(endpoint.body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=endpoint.method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                status = resp.status
        except HTTPError as exc:
            status = exc.code
            error = f"HTTP {exc.code}"
            try:
                body = exc.read()
            except Exception:
                # Keep the HTTP-status attribution even when the error body
                # itself is truncated or unreadable.
                body = b""
    except URLError as exc:
        error = f"URLError: {exc.reason}"
    except Exception as exc:  # ConnectionResetError, IncompleteRead, InvalidURL, ...
        error = f"{type(exc).__name__}: {exc}"
    end = time.perf_counter()
    if error is None and 200 <= status < 300:
        text = body.decode("utf-8", errors="replace")
        error = validate_response_shape(endpoint, text)
    return {
        "endpoint": endpoint.name,
        "status": status,
        "ok": error is None and 200 <= status < 300,
        "latency_ms": (end - scheduled_start) * 1000.0,
        "service_time_ms": (end - dequeued) * 1000.0,
        "queue_wait_ms": (dequeued - scheduled_start) * 1000.0,
        "bytes": len(body),
        "error": error,
    }


def validate_response_shape(endpoint: Endpoint, text: str) -> str | None:
    if endpoint.expect_text is not None:
        if endpoint.expect_text not in text:
            return f"unexpected response text for {endpoint.name}: {text[:120]!r}"
        return None
    if not endpoint.expect_json:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"non-JSON response for {endpoint.name}: {text[:120]!r}"
    if isinstance(payload, list):
        return None
    if isinstance(payload, dict):
        # Train Ticket services wrap results in edu.fudan.common.util.Response
        # {status, msg, data} where status 1 means success. An HTTP-200 body
        # with status 0 is a service-level failure, not a successful sample.
        if "status" in payload:
            if payload.get("status") != 1:
                msg = payload.get("msg", payload.get("message"))
                return f"error envelope for {endpoint.name}: status={payload.get('status')!r} msg={msg!r}"
            return None
        if "data" in payload:
            return None
        return f"unexpected JSON object for {endpoint.name}: keys={sorted(payload)[:8]}"
    return f"unexpected JSON type for {endpoint.name}: {type(payload).__name__}"


def latency_stats(latencies: list[float]) -> dict[str, float | None]:
    return {
        "mean": statistics.fmean(latencies) if latencies else None,
        "p50": percentile(latencies, 50),
        "p90": percentile(latencies, 90),
        "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99),
        "max": max(latencies) if latencies else None,
    }


def summarize(results: list[dict[str, Any]], elapsed_s: float) -> dict[str, Any]:
    successes = [r for r in results if r["ok"]]
    failures = [r for r in results if not r["ok"]]
    latencies = [r["latency_ms"] for r in successes if r["latency_ms"] is not None]
    service_times = [r["service_time_ms"] for r in successes if r["service_time_ms"] is not None]
    queue_waits = [r["queue_wait_ms"] for r in results if r.get("queue_wait_ms") is not None]
    by_endpoint: dict[str, dict[str, Any]] = {}
    for r in results:
        bucket = by_endpoint.setdefault(
            r["endpoint"],
            {"requests": 0, "successes": 0, "failures": 0, "_latencies": []},
        )
        bucket["requests"] += 1
        if r["ok"]:
            bucket["successes"] += 1
            if r["latency_ms"] is not None:
                bucket["_latencies"].append(r["latency_ms"])
        else:
            bucket["failures"] += 1
    for bucket in by_endpoint.values():
        lats = bucket.pop("_latencies")
        bucket["latency_ms"] = {
            "mean": statistics.fmean(lats) if lats else None,
            "p50": percentile(lats, 50),
            "p95": percentile(lats, 95),
        }
    errors_by_type: dict[str, int] = {}
    for r in failures:
        key = (r["error"] or f"HTTP {r['status']}").split(": ", 1)[0][:80]
        errors_by_type[key] = errors_by_type.get(key, 0) + 1
    return {
        "headline_metric": "requests_per_second",
        "requests_per_second": len(successes) / elapsed_s if elapsed_s > 0 else 0.0,
        "attempted_requests_per_second": len(results) / elapsed_s if elapsed_s > 0 else 0.0,
        "total_requests": len(results),
        "successful_requests": len(successes),
        "failed_requests": len(failures),
        "error_rate": (len(failures) / len(results)) if results else 0.0,
        "timeout_failures": sum(1 for r in failures if "timed out" in (r["error"] or "")),
        # Latency of successful requests measured from their scheduled send
        # time (open loop: client-side queue wait counts). service_time_ms is
        # the HTTP exchange alone. Failed requests (incl. timeouts) are not in
        # either distribution; check error_rate before comparing latencies.
        "latency_ms": latency_stats(latencies),
        "service_time_ms": latency_stats(service_times),
        "queue_wait_ms": {
            "mean": statistics.fmean(queue_waits) if queue_waits else None,
            "p99": percentile(queue_waits, 99),
            "max": max(queue_waits) if queue_waits else None,
        },
        "by_endpoint": by_endpoint,
        "errors_by_type": errors_by_type,
        "sample_errors": failures[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a running Train Ticket deployment.")
    parser.add_argument(
        "--base-url", default="http://localhost:8080", help="UI proxy or gateway base URL"
    )
    parser.add_argument("--rate", type=float, default=10.0, help="target request rate per second")
    parser.add_argument(
        "--duration", type=float, default=30.0, help="benchmark duration in seconds"
    )
    parser.add_argument(
        "--concurrency", type=int, default=32, help="maximum concurrent in-flight requests"
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    parser.add_argument("--seed", type=int, default=1, help="random seed for endpoint selection")
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=0.0,
        help="exit non-zero when error_rate exceeds this fraction (default: any failure fails)",
    )
    parser.add_argument(
        "--direct-services",
        action="store_true",
        help="bypass the gateway and route each service path to its exposed local service port",
    )
    parser.add_argument("--output-json", default=None, help="optional path for structured results")
    args = parser.parse_args()

    if args.rate <= 0:
        raise SystemExit("--rate must be > 0")
    if args.duration <= 0:
        raise SystemExit("--duration must be > 0")
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be > 0")

    random.seed(args.seed)
    base_url = normalize_base_url(args.base_url)
    population = [endpoint for endpoint in ENDPOINTS for _ in range(endpoint.weight)]
    total = max(1, int(args.rate * args.duration))
    interval = 1.0 / args.rate
    results: list[dict[str, Any]] = []
    futures: list[concurrent.futures.Future] = []
    future_endpoint: dict[concurrent.futures.Future, str] = {}
    interrupted = False
    submit_end = None

    def collect(fut: concurrent.futures.Future) -> None:
        try:
            results.append(fut.result())
        except Exception as exc:  # request_once never raises; last-resort guard
            results.append(
                {
                    "endpoint": future_endpoint.get(fut, "?"),
                    "status": 0,
                    "ok": False,
                    "latency_ms": None,
                    "service_time_ms": None,
                    "queue_wait_ms": None,
                    "bytes": 0,
                    "error": f"internal: {type(exc).__name__}: {exc}",
                }
            )

    start = time.perf_counter()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency)
    try:
        for i in range(total):
            target_time = start + i * interval
            sleep_s = target_time - time.perf_counter()
            if sleep_s > 0:
                time.sleep(sleep_s)
            endpoint = random.choice(population)
            fut = pool.submit(
                request_once, base_url, endpoint, args.timeout, args.direct_services, target_time
            )
            future_endpoint[fut] = endpoint.name
            futures.append(fut)
        submit_end = time.perf_counter()
        for fut in concurrent.futures.as_completed(futures):
            collect(fut)
        pool.shutdown(wait=True)
    except KeyboardInterrupt:
        interrupted = True
        print(
            "Interrupted: cancelling queued requests, waiting for in-flight ones...",
            file=sys.stderr,
        )
        pool.shutdown(wait=False, cancel_futures=True)
        concurrent.futures.wait(
            [f for f in futures if not f.done()], timeout=args.timeout
        )
        results.clear()
        for fut in futures:
            if fut.done() and not fut.cancelled():
                collect(fut)

    elapsed_s = time.perf_counter() - start
    summary = summarize(results, elapsed_s)

    offered_rate = None
    if submit_end is not None and submit_end > start and total > 1:
        # total arrivals span (total - 1) inter-arrival intervals.
        offered_rate = (total - 1) / (submit_end - start)
        if offered_rate < 0.95 * args.rate:
            print(
                f"WARNING: submission fell behind schedule: offered {offered_rate:.1f} req/s "
                f"vs target {args.rate:.1f} req/s; results understate the requested load.",
                file=sys.stderr,
            )
    summary.update(
        {
            "base_url": base_url.rstrip("/"),
            "direct_services": args.direct_services,
            "target_rate": args.rate,
            "offered_rate": offered_rate,
            "duration_s": args.duration,
            "elapsed_s": elapsed_s,
            "concurrency": args.concurrency,
            "interrupted": interrupted,
        }
    )
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    if interrupted:
        return 130
    return 1 if summary["error_rate"] > args.max_error_rate else 0


if __name__ == "__main__":
    raise SystemExit(main())
