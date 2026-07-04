#!/usr/bin/env python3
"""Read-only deployment checker for the Train Ticket microservice app."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Check:
    name: str
    method: str
    path: str
    expect_json: bool = False
    expect_text: str | None = None
    expect_items: bool = False
    body: Any | None = None


CHECKS = [
    Check("config welcome", "GET", "/api/v1/configservice/welcome", expect_text="Config Service"),
    Check(
        "station welcome", "GET", "/api/v1/stationservice/welcome", expect_text="Station Service"
    ),
    Check(
        "train welcome", "GET", "/api/v1/trainservice/trains/welcome", expect_text="Train Service"
    ),
    Check("travel welcome", "GET", "/api/v1/travelservice/welcome", expect_text="Travel Service"),
    Check("route welcome", "GET", "/api/v1/routeservice/welcome", expect_text="Route Service"),
    Check(
        "price welcome", "GET", "/api/v1/priceservice/prices/welcome", expect_text="Price Service"
    ),
    Check(
        "stations list",
        "GET",
        "/api/v1/stationservice/stations",
        expect_json=True,
        expect_items=True,
    ),
    Check("trains list", "GET", "/api/v1/trainservice/trains", expect_json=True, expect_items=True),
    Check("trips list", "GET", "/api/v1/travelservice/trips", expect_json=True, expect_items=True),
    Check("routes list", "GET", "/api/v1/routeservice/routes", expect_json=True, expect_items=True),
    Check("prices list", "GET", "/api/v1/priceservice/prices", expect_json=True, expect_items=True),
    Check("configs list", "GET", "/api/v1/configservice/configs", expect_json=True),
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


def check_url(base_url: str, check: Check, direct_services: bool) -> str:
    if not direct_services:
        return urljoin(base_url, check.path.lstrip("/"))
    parts = check.path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "api" or parts[1] != "v1":
        raise RuntimeError(f"{check.name}: cannot map path to direct service URL: {check.path}")
    service = parts[2]
    port = DIRECT_SERVICE_PORTS.get(service)
    if port is None:
        raise RuntimeError(f"{check.name}: no direct port mapping for service {service!r}")
    base = urlsplit(base_url)
    host = base.hostname or "localhost"
    netloc = f"{host}:{port}"
    return urlunsplit((base.scheme or "http", netloc, check.path, "", ""))


def request_json_or_text(
    base_url: str,
    check: Check,
    timeout: float,
    direct_services: bool,
) -> tuple[int, str, Any | None, float]:
    url = check_url(base_url, check, direct_services)
    data = None
    headers = {"Accept": "application/json,text/plain,*/*"}
    if check.body is not None:
        data = json.dumps(check.body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=check.method)
    start = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except URLError as exc:
        raise RuntimeError(f"{check.name}: request failed: {exc}") from exc
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    parsed = None
    if check.expect_json:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{check.name}: response is not valid JSON: {raw[:200]!r}") from exc
    return status, raw, parsed, elapsed_ms


def extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if data is not None:
            return [data]
    return []


def run_check(
    base_url: str,
    check: Check,
    timeout: float,
    allow_empty: bool,
    direct_services: bool,
) -> dict[str, Any]:
    status, raw, parsed, elapsed_ms = request_json_or_text(
        base_url, check, timeout, direct_services
    )
    if not (200 <= status < 300):
        raise RuntimeError(f"{check.name}: HTTP {status}: {raw[:300]!r}")
    if check.expect_text and check.expect_text not in raw:
        raise RuntimeError(f"{check.name}: expected text {check.expect_text!r}, got {raw[:200]!r}")
    item_count = None
    if check.expect_items:
        items = extract_items(parsed)
        item_count = len(items)
        if not allow_empty and item_count == 0:
            raise RuntimeError(f"{check.name}: expected seeded data, got empty response")
    return {
        "name": check.name,
        "status": status,
        "latency_ms": round(elapsed_ms, 3),
        "item_count": item_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a running Train Ticket deployment.")
    parser.add_argument(
        "--base-url", default="http://localhost:8080", help="UI proxy or gateway base URL"
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    parser.add_argument(
        "--allow-empty", action="store_true", help="allow list endpoints to return no seeded data"
    )
    parser.add_argument(
        "--direct-services",
        action="store_true",
        help="bypass the gateway and route each service path to its exposed local service port",
    )
    parser.add_argument("--output-json", default=None, help="optional path for structured results")
    args = parser.parse_args()

    base_url = normalize_base_url(args.base_url)
    results = []
    failures = []
    for check in CHECKS:
        try:
            result = run_check(
                base_url, check, args.timeout, args.allow_empty, args.direct_services
            )
            results.append(result)
            print(f"PASS {check.name} ({result['latency_ms']:.1f} ms)")
        except Exception as exc:
            failures.append({"name": check.name, "error": str(exc)})
            print(f"FAIL {check.name}: {exc}", file=sys.stderr)

    summary = {
        "base_url": base_url.rstrip("/"),
        "direct_services": args.direct_services,
        "passed": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    print(json.dumps({"passed": summary["passed"], "failed": summary["failed"]}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
