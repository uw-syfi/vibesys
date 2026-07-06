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
    # Keys every returned item must have. Restricted to fields that exist in
    # both the 0.2.0 prebuilt images and the v1.0.0 source entities.
    required_item_keys: tuple[str, ...] = ()
    body: Any | None = None


CHECKS = [
    Check(
        "config welcome",
        "GET",
        "/api/v1/configservice/welcome",
        expect_text="Welcome to [ Config Service ]",
    ),
    Check(
        "station welcome",
        "GET",
        "/api/v1/stationservice/welcome",
        expect_text="Welcome to [ Station Service ]",
    ),
    Check(
        "train welcome",
        "GET",
        "/api/v1/trainservice/trains/welcome",
        expect_text="Welcome to [ Train Service ]",
    ),
    Check(
        "travel welcome",
        "GET",
        "/api/v1/travelservice/welcome",
        expect_text="Welcome to [ Travel Service ]",
    ),
    Check(
        "route welcome",
        "GET",
        "/api/v1/routeservice/welcome",
        expect_text="Welcome to [ Route Service ]",
    ),
    Check(
        "price welcome",
        "GET",
        "/api/v1/priceservice/prices/welcome",
        expect_text="Welcome to [ Price Service ]",
    ),
    Check(
        "stations list",
        "GET",
        "/api/v1/stationservice/stations",
        expect_json=True,
        expect_items=True,
        required_item_keys=("id", "name"),
    ),
    Check(
        "trains list",
        "GET",
        "/api/v1/trainservice/trains",
        expect_json=True,
        expect_items=True,
        required_item_keys=("id", "averageSpeed"),
    ),
    Check(
        "trips list",
        "GET",
        "/api/v1/travelservice/trips",
        expect_json=True,
        expect_items=True,
        required_item_keys=("tripId", "routeId"),
    ),
    Check(
        "routes list",
        "GET",
        "/api/v1/routeservice/routes",
        expect_json=True,
        expect_items=True,
        required_item_keys=("id", "stations"),
    ),
    Check(
        "prices list",
        "GET",
        "/api/v1/priceservice/prices",
        expect_json=True,
        expect_items=True,
        required_item_keys=("id", "trainType", "routeId", "basicPriceRate"),
    ),
    Check(
        "configs list",
        "GET",
        "/api/v1/configservice/configs",
        expect_json=True,
        expect_items=True,
        required_item_keys=("name", "value"),
    ),
]

# Referential-integrity checks over the seeded data. Field names are looked up
# per item because 0.2.0 and v1.0.0 use different names for some references;
# a check silently skips when the source field is absent. Targets may list
# several keys: v1.0.0 TrainType has a random UUID id and keeps the human name
# in a separate "name" field that price configs reference, so train references
# match against the union of both.
CONSISTENCY_CHECKS = [
    ("trips reference routes", "trips list", ("routeId",), "routes list", ("id",)),
    ("prices reference routes", "prices list", ("routeId",), "routes list", ("id",)),
    ("prices reference trains", "prices list", ("trainType",), "trains list", ("id", "name")),
    (
        "trips reference stations",
        "trips list",
        ("startingStationId", "terminalStationId"),
        "stations list",
        ("id",),
    ),
    ("trips reference trains", "trips list", ("trainTypeId",), "trains list", ("id", "name")),
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
    host = format_host(base.hostname or "localhost")
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
    if check.expect_json or check.expect_items:
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


def validate_envelope(check: Check, parsed: Any, allow_empty: bool) -> None:
    """Enforce the edu.fudan.common.util.Response convention: status 1 = success.

    An HTTP 200 body like {"status":0,"msg":"error","data":null} is a
    service-level failure and must not pass. The only tolerated non-1 status is
    the "No content" empty result, and only under --allow-empty.
    """
    if not isinstance(parsed, dict) or "status" not in parsed:
        return
    status = parsed.get("status")
    if status == 1:
        return
    if allow_empty and check.expect_items and not extract_items(parsed):
        return
    raise RuntimeError(
        f"{check.name}: response envelope status={status!r} msg={parsed.get('msg')!r}"
    )


def validate_items(check: Check, items: list[Any]) -> None:
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise RuntimeError(
                f"{check.name}: item {idx} is {type(item).__name__}, expected object"
            )
        missing = [key for key in check.required_item_keys if key not in item]
        if missing:
            raise RuntimeError(
                f"{check.name}: item {idx} is missing expected fields {missing}: "
                f"{json.dumps(item)[:200]}"
            )


def run_check(
    base_url: str,
    check: Check,
    timeout: float,
    allow_empty: bool,
    direct_services: bool,
) -> tuple[dict[str, Any], list[Any]]:
    status, raw, parsed, elapsed_ms = request_json_or_text(
        base_url, check, timeout, direct_services
    )
    if not (200 <= status < 300):
        raise RuntimeError(f"{check.name}: HTTP {status}: {raw[:300]!r}")
    if check.expect_text and check.expect_text not in raw:
        raise RuntimeError(f"{check.name}: expected text {check.expect_text!r}, got {raw[:200]!r}")
    validate_envelope(check, parsed, allow_empty)
    item_count = None
    items: list[Any] = []
    if check.expect_items:
        items = extract_items(parsed)
        item_count = len(items)
        if not allow_empty and item_count == 0:
            raise RuntimeError(f"{check.name}: expected seeded data, got empty response")
        validate_items(check, items)
    result = {
        "name": check.name,
        "status": status,
        "latency_ms": round(elapsed_ms, 3),
        "item_count": item_count,
    }
    return result, items


def run_consistency_checks(
    collected: dict[str, list[Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Cross-endpoint referential integrity over whatever data was fetched.

    A given check runs only when both sides returned data and the reference
    field exists in the source items (field names differ across Train Ticket
    versions); otherwise it is skipped.
    """
    results = []
    failures = []
    for name, src_name, ref_keys, dst_name, dst_keys in CONSISTENCY_CHECKS:
        src_items = collected.get(src_name, [])
        dst_items = collected.get(dst_name, [])
        refs = {
            item[key]
            for item in src_items
            if isinstance(item, dict)
            for key in ref_keys
            if item.get(key) is not None
        }
        targets = {
            item[key]
            for item in dst_items
            if isinstance(item, dict)
            for key in dst_keys
            if item.get(key) is not None
        }
        if not refs or not targets:
            continue
        dangling = sorted(str(r) for r in refs - targets)
        if dangling:
            failures.append(
                {
                    "name": name,
                    "error": f"{name}: dangling references not in {dst_name}: {dangling[:5]}",
                }
            )
        else:
            results.append({"name": name, "status": None, "latency_ms": None, "item_count": len(refs)})
    return results, failures


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
    collected: dict[str, list[Any]] = {}
    for check in CHECKS:
        try:
            result, items = run_check(
                base_url, check, args.timeout, args.allow_empty, args.direct_services
            )
            results.append(result)
            collected[check.name] = items
            print(f"PASS {check.name} ({result['latency_ms']:.1f} ms)")
        except Exception as exc:
            failures.append({"name": check.name, "error": str(exc)})
            print(f"FAIL {check.name}: {exc}", file=sys.stderr)

    consistency_results, consistency_failures = run_consistency_checks(collected)
    for result in consistency_results:
        results.append(result)
        print(f"PASS {result['name']} ({result['item_count']} references)")
    for failure in consistency_failures:
        failures.append(failure)
        print(f"FAIL {failure['name']}: {failure['error']}", file=sys.stderr)

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
