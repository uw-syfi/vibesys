from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import psutil

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "reference" / "config.json"

LOAD_LEVELS = {
    "light":  {"concurrent_clients": 5,  "total_reservations": 50},
    "medium": {"concurrent_clients": 20, "total_reservations": 200},
    "heavy":  {"concurrent_clients": 50, "total_reservations": 500},
}



# Helpers

def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * pct / 100)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


def emit(metric: str, value) -> None:
    print(json.dumps({"metric": metric, "value": value}), flush=True)



async def reset(client: httpx.AsyncClient, base_url: str) -> None:
    await client.post(f"{base_url}/reset")


async def setup_hotels(client: httpx.AsyncClient, base_url: str, cfg: dict) -> List[dict]:
    s = cfg["setup"]
    hotels = []
    for i in range(s["num_hotels"]):
        rooms = []
        for rt, count in s["rooms_per_type"].items():
            for _ in range(count):
                rooms.append({
                    "room_type": rt,
                    "capacity": 1,
                    "rate_per_night": s["rate_per_night"][rt],
                })
        resp = await client.post(f"{base_url}/hotels", json={
            "name": f"Hotel {i}",
            "location": f"City {i}",
            "star_rating": 3,
            "rooms": rooms,
        })
        if resp.status_code != 201:
            raise RuntimeError(f"Setup failed: POST /hotels → {resp.status_code}")
        hotels.append(resp.json())
    return hotels


async def setup_users(client: httpx.AsyncClient, base_url: str, n: int) -> List[dict]:
    users = []
    for i in range(n):
        resp = await client.post(
            f"{base_url}/users",
            json={"name": f"User {i}", "email": f"user{i}@bench.com"},
        )
        if resp.status_code != 201:
            raise RuntimeError(f"Setup failed: POST /users → {resp.status_code}")
        users.append(resp.json())
    return users


async def reservation_worker(
    client: httpx.AsyncClient,
    base_url: str,
    hotel: dict,
    user: dict,
    room_type: str,
    check_in: str,
    check_out: str,
    latencies_ms: List[float],
    results: Dict[str, int],
) -> Optional[str]:
    t0 = time.perf_counter()
    try:
        resp = await client.post(f"{base_url}/reservations", json={
            "user_id": user["user_id"],
            "hotel_id": hotel["hotel_id"],
            "room_type": room_type,
            "check_in": check_in,
            "check_out": check_out,
        })
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)
        if resp.status_code == 201:
            results["success"] = results.get("success", 0) + 1
            return resp.json()["reservation_id"]
        else:
            results["error"] = results.get("error", 0) + 1
            return None
    except Exception:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)
        results["error"] = results.get("error", 0) + 1
        return None


async def read_worker(
    client: httpx.AsyncClient,
    base_url: str,
    hotel_id: str,
    check_in: str,
    check_out: str,
    latencies_ms: List[float],
    results: Dict[str, int],
) -> None:
    t0 = time.perf_counter()
    try:
        resp = await client.get(
            f"{base_url}/hotels/{hotel_id}/availability",
            params={"check_in": check_in, "check_out": check_out},
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)
        if resp.status_code == 200:
            results["reads_ok"] = results.get("reads_ok", 0) + 1
        else:
            results["reads_err"] = results.get("reads_err", 0) + 1
    except Exception:
        results["reads_err"] = results.get("reads_err", 0) + 1


async def run_benchmark(base_url: str, cfg: dict, load: dict, seed: int = 42) -> dict:
    rng = random.Random(seed)
    n_clients = load["concurrent_clients"]
    n_reservations = load["total_reservations"]
    today = date.today()
    max_days = cfg["benchmark"]["date_range_days"]
    max_stay = cfg["benchmark"]["max_stay_nights"]
    read_fraction = cfg["benchmark"]["read_fraction"]
    warmup = cfg["benchmark"]["warmup_requests"]

    latencies_ms: List[float] = []
    results: Dict[str, int] = {}
    confirmed_ids: List[str] = []
    confirmed_room_ids: Dict[str, Tuple[str, str, str]] = {}  # res_id is of the form (room_id, check_in, check_out)

    async with httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=n_clients + 10)) as client:
        await reset(client, base_url)
        hotels = await setup_hotels(client, base_url, cfg)
        users = await setup_users(client, base_url, cfg["setup"]["num_users"])

        for _ in range(warmup):
            hotel = rng.choice(hotels)
            user = rng.choice(users)
            offset = rng.randint(1, max_days)
            stay = rng.randint(1, max_stay)
            ci = (today + timedelta(days=offset)).isoformat()
            co = (today + timedelta(days=offset + stay)).isoformat()
            try:
                await client.post(f"{base_url}/reservations", json={
                    "user_id": user["user_id"],
                    "hotel_id": hotel["hotel_id"],
                    "room_type": "single",
                    "check_in": ci,
                    "check_out": co,
                })
            except Exception:
                pass

        await reset(client, base_url)
        hotels = await setup_hotels(client, base_url, cfg)
        users = await setup_users(client, base_url, cfg["setup"]["num_users"])

        tasks = []
        for i in range(n_reservations):
            hotel = rng.choice(hotels)
            user = rng.choice(users)
            rt = rng.choice(list(cfg["setup"]["rooms_per_type"].keys()))
            offset = rng.randint(1, max_days)
            stay = rng.randint(1, max_stay)
            ci = (today + timedelta(days=offset)).isoformat()
            co = (today + timedelta(days=offset + stay)).isoformat()
            tasks.append(("reservation", hotel, user, rt, ci, co))

            if rng.random() < read_fraction:
                tasks.append(("read", hotel["hotel_id"], ci, co))

        rng.shuffle(tasks)
        proc = psutil.Process()
        cpu_before = proc.cpu_percent(interval=None)
        mem_before = proc.memory_info().rss
        semaphore = asyncio.Semaphore(n_clients)
        wall_start = time.perf_counter()

        async def bounded_task(task):
            async with semaphore:
                if task[0] == "reservation":
                    _, hotel, user, rt, ci, co = task
                    res_id = await reservation_worker(
                        client, base_url, hotel, user, rt, ci, co, latencies_ms, results
                    )
                    if res_id:
                        confirmed_ids.append(res_id)
                else:
                    _, hotel_id, ci, co = task
                    await read_worker(client, base_url, hotel_id, ci, co, latencies_ms, results)

        await asyncio.gather(*[bounded_task(t) for t in tasks])
        wall_elapsed = time.perf_counter() - wall_start

        cpu_after = proc.cpu_percent(interval=None)
        mem_after = proc.memory_info().rss

        room_night_count: Dict[Tuple[str, str], int] = {}
        for res_id in confirmed_ids:
            try:
                resp = await client.get(f"{base_url}/reservations/{res_id}")
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("status") == "confirmed":
                        ci_d = date.fromisoformat(body["check_in"])
                        co_d = date.fromisoformat(body["check_out"])
                        room_id = body["room_id"]
                        nights = (co_d - ci_d).days
                        for n in range(nights):
                            night = (ci_d + timedelta(days=n)).isoformat()
                            key = (room_id, night)
                            room_night_count[key] = room_night_count.get(key, 0) + 1
            except Exception:
                pass

        overbooking_violations = sum(1 for v in room_night_count.values() if v > 1)

    # Metrics

    n_reservation_tasks = sum(1 for t in tasks if t[0] == "reservation")
    success_count = results.get("success", 0)
    error_count = results.get("error", 0)

    sorted_lat = sorted(latencies_ms)
    p50 = percentile(sorted_lat, 50)
    p95 = percentile(sorted_lat, 95)
    p99 = percentile(sorted_lat, 99)

    throughput_rps = len(latencies_ms) / wall_elapsed if wall_elapsed > 0 else 0
    reservations_per_sec = success_count / wall_elapsed if wall_elapsed > 0 else 0

    cpu_pct = max(cpu_before, cpu_after)
    mem_mb = max(mem_before, mem_after) / (1024 * 1024)

    return {
        "reservations_per_sec": round(reservations_per_sec, 2),
        "overbooking_violations": overbooking_violations,
        "success_count": success_count,
        "error_count": error_count,
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
        "throughput_rps": round(throughput_rps, 2),
        "cpu_percent": round(cpu_pct, 1),
        "memory_mb": round(mem_mb, 1),
        "wall_time_sec": round(wall_elapsed, 3),
        "total_attempts": n_reservation_tasks,
    }




def main() -> None:
    parser = argparse.ArgumentParser(description="Hotel Reservation benchmark")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--load-level", choices=list(LOAD_LEVELS.keys()), default="medium"
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    cfg = load_config(args.config)
    load = LOAD_LEVELS[args.load_level]
    bench_cfg = cfg.get("benchmark", {})

    print(f"# Hotel Reservation Benchmark — load_level={args.load_level}", flush=True)
    print(f"# concurrent_clients={load['concurrent_clients']}  "
          f"total_reservations={load['total_reservations']}", flush=True)

    metrics = asyncio.run(run_benchmark(args.base_url, cfg, load, seed=args.seed))

    for key, val in metrics.items():
        emit(key, val)
    print(json.dumps({"summary": metrics}), flush=True)
    if metrics["overbooking_violations"] > 0:
        print(
            f"BENCHMARK FAILED: {metrics['overbooking_violations']} overbooking violation(s) detected.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"\nPrimary metric (reservations_per_sec): {metrics['reservations_per_sec']:.2f}",
        flush=True,
    )


if __name__ == "__main__":
    main()
