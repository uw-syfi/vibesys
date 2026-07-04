#!/usr/bin/env python3
"""Read-only HTTP benchmark for the Train Ticket microservice app."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import random
import statistics
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
        expect_text="Travel Service",
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
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw.rstrip("/") + "/"


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
    host = base.hostname or "localhost"
    return urlunsplit((base.scheme or "http", f"{host}:{port}", endpoint.path, "", ""))


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def request_once(
    base_url: str, endpoint: Endpoint, timeout: float, direct_services: bool
) -> dict[str, Any]:
    url = endpoint_url(base_url, endpoint, direct_services)
    headers = {"Accept": "application/json,text/plain,*/*"}
    data = None
    if endpoint.body is not None:
        data = json.dumps(endpoint.body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=endpoint.method)
    start = time.perf_counter()
    status = 0
    size = 0
    error = None
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            size = len(body)
    except HTTPError as exc:
        body = exc.read()
        status = exc.code
        size = len(body)
        error = f"HTTP {exc.code}"
    except URLError as exc:
        error = str(exc)
    except TimeoutError as exc:
        error = str(exc)
    if error is None and 200 <= status < 300:
        text = body.decode("utf-8", errors="replace")
        shape_error = validate_response_shape(endpoint, text)
        if shape_error:
            error = shape_error
    latency_ms = (time.perf_counter() - start) * 1000.0
    ok = error is None and 200 <= status < 300
    return {
        "endpoint": endpoint.name,
        "status": status,
        "ok": ok,
        "latency_ms": latency_ms,
        "bytes": size,
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
        # Some Train Ticket services wrap lists in edu.fudan.common.util.Response.
        if (
            "data" in payload
            or ("status" in payload and "msg" in payload)
            or ("status" in payload and "message" in payload)
        ):
            return None
        return f"unexpected JSON object for {endpoint.name}: keys={sorted(payload)[:8]}"
    return f"unexpected JSON type for {endpoint.name}: {type(payload).__name__}"


def summarize(results: list[dict[str, Any]], elapsed_s: float) -> dict[str, Any]:
    successes = [r for r in results if r["ok"]]
    failures = [r for r in results if not r["ok"]]
    latencies = [r["latency_ms"] for r in successes]
    by_endpoint: dict[str, dict[str, Any]] = {}
    for r in results:
        bucket = by_endpoint.setdefault(
            r["endpoint"], {"requests": 0, "successes": 0, "failures": 0}
        )
        bucket["requests"] += 1
        if r["ok"]:
            bucket["successes"] += 1
        else:
            bucket["failures"] += 1
    return {
        "headline_metric": "requests_per_second",
        "requests_per_second": len(successes) / elapsed_s if elapsed_s > 0 else 0.0,
        "attempted_requests_per_second": len(results) / elapsed_s if elapsed_s > 0 else 0.0,
        "total_requests": len(results),
        "successful_requests": len(successes),
        "failed_requests": len(failures),
        "error_rate": (len(failures) / len(results)) if results else 0.0,
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else None,
            "p50": percentile(latencies, 50),
            "p90": percentile(latencies, 90),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else None,
        },
        "by_endpoint": by_endpoint,
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

    start = time.perf_counter()
    futures: list[concurrent.futures.Future] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for i in range(total):
            target_time = start + i * interval
            sleep_s = target_time - time.perf_counter()
            if sleep_s > 0:
                time.sleep(sleep_s)
            endpoint = random.choice(population)
            futures.append(
                pool.submit(request_once, base_url, endpoint, args.timeout, args.direct_services)
            )
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    elapsed_s = time.perf_counter() - start
    summary = summarize(results, elapsed_s)
    summary.update(
        {
            "base_url": base_url.rstrip("/"),
            "direct_services": args.direct_services,
            "target_rate": args.rate,
            "duration_s": args.duration,
            "elapsed_s": elapsed_s,
            "concurrency": args.concurrency,
        }
    )
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed_requests"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
