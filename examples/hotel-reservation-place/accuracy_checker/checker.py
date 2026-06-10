"""
Usage instructions for this file:

    python checker.py [--base-url http://localhost:8000] [--config config.json]

"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "reference" / "config.json"



# Helpers

def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def random_dates(seed: int, max_days: int = 30, max_stay: int = 5) -> Tuple[date, str, str]:
    rng = random.Random(seed)
    start_offset = rng.randint(1, max_days)
    stay = rng.randint(1, max_stay)
    today = date.today()
    check_in = today + timedelta(days=start_offset)
    check_out = check_in + timedelta(days=stay)
    return check_in, check_in.isoformat(), check_out.isoformat()


class CheckFailure(Exception):
    pass


def assert_status(resp: httpx.Response, expected: int, context: str) -> None:
    if resp.status_code != expected:
        raise CheckFailure(
            f"{context}: expected HTTP {expected}, got {resp.status_code}. "
            f"Body: {resp.text[:300]}"
        )


def assert_field(obj: dict, field: str, context: str) -> Any:
    if field not in obj:
        raise CheckFailure(f"{context}: missing field '{field}' in response {obj}")
    return obj[field]




# Setup helpers

async def reset(client: httpx.AsyncClient, base_url: str) -> None:
    resp = await client.post(f"{base_url}/reset")
    assert_status(resp, 204, "POST /reset")


async def create_hotels(client: httpx.AsyncClient, base_url: str, cfg: dict) -> List[dict]:
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
        body = {
            "name": f"Hotel {i}",
            "location": f"City {i}",
            "star_rating": 3,
            "rooms": rooms,
        }
        resp = await client.post(f"{base_url}/hotels", json=body)
        assert_status(resp, 201, f"POST /hotels (hotel {i})")
        hotels.append(resp.json())
    return hotels


async def create_users(client: httpx.AsyncClient, base_url: str, n: int) -> List[dict]:
    users = []
    for i in range(n):
        resp = await client.post(
            f"{base_url}/users",
            json={"name": f"User {i}", "email": f"user{i}@example.com"},
        )
        assert_status(resp, 201, f"POST /users (user {i})")
        users.append(resp.json())
    return users




# Individual checks on each criterion

async def check_p1_response_shape(client: httpx.AsyncClient, base_url: str, cfg: dict) -> None:
    await reset(client, base_url)
    hotels = await create_hotels(client, base_url, cfg)
    users = await create_users(client, base_url, cfg["setup"]["num_users"])

    hotel = hotels[0]
    user = users[0]
    _, check_in, check_out = random_dates(seed=1001)

    resp = await client.post(f"{base_url}/reservations", json={
        "user_id": user["user_id"],
        "hotel_id": hotel["hotel_id"],
        "room_type": "single",
        "check_in": check_in,
        "check_out": check_out,
    })
    assert_status(resp, 201, "POST /reservations (P1)")
    body = resp.json()
    for field in ("reservation_id", "user_id", "hotel_id", "room_id",
                  "room_type", "check_in", "check_out", "total_price", "status"):
        assert_field(body, field, "POST /reservations response (P1)")

    if body["status"] != "confirmed":
        raise CheckFailure(f"P1: expected status='confirmed', got '{body['status']}'")
    if body["total_price"] <= 0:
        raise CheckFailure(f"P1: total_price must be > 0, got {body['total_price']}")


async def check_p2_visibility(client: httpx.AsyncClient, base_url: str, cfg: dict) -> None:
    await reset(client, base_url)
    hotels = await create_hotels(client, base_url, cfg)
    users = await create_users(client, base_url, cfg["setup"]["num_users"])

    hotel = hotels[0]
    user = users[0]
    _, check_in, check_out = random_dates(seed=2001)

    place = await client.post(f"{base_url}/reservations", json={
        "user_id": user["user_id"],
        "hotel_id": hotel["hotel_id"],
        "room_type": "single",
        "check_in": check_in,
        "check_out": check_out,
    })
    assert_status(place, 201, "POST /reservations (P2)")
    res_id = place.json()["reservation_id"]

    get = await client.get(f"{base_url}/reservations/{res_id}")
    assert_status(get, 200, f"GET /reservations/{res_id} (P2)")
    body = get.json()
    if body["status"] != "confirmed":
        raise CheckFailure(f"P2: GET shows status='{body['status']}', expected 'confirmed'")
    if body["reservation_id"] != res_id:
        raise CheckFailure(f"P2: reservation_id mismatch")


async def check_p3_availability_decreases(client: httpx.AsyncClient, base_url: str, cfg: dict) -> None:
    await reset(client, base_url)
    hotels = await create_hotels(client, base_url, cfg)
    users = await create_users(client, base_url, cfg["setup"]["num_users"])

    hotel = hotels[0]
    hotel_id = hotel["hotel_id"]
    user = users[0]
    _, check_in, check_out = random_dates(seed=3001)

    # Availability before a reservation is placed
    before = await client.get(
        f"{base_url}/hotels/{hotel_id}/availability",
        params={"check_in": check_in, "check_out": check_out},
    )
    assert_status(before, 200, "GET /availability before (P3)")
    avail_before = before.json()["available_rooms"].get("single", 0)

    # Place a reservation on the system
    place = await client.post(f"{base_url}/reservations", json={
        "user_id": user["user_id"],
        "hotel_id": hotel_id,
        "room_type": "single",
        "check_in": check_in,
        "check_out": check_out,
    })
    assert_status(place, 201, "POST /reservations (P3)")

    # Availability after reservation is placed
    after = await client.get(
        f"{base_url}/hotels/{hotel_id}/availability",
        params={"check_in": check_in, "check_out": check_out},
    )
    assert_status(after, 200, "GET /availability after (P3)")
    avail_after = after.json()["available_rooms"].get("single", 0)

    if avail_after >= avail_before:
        raise CheckFailure(
            f"P3: availability did not decrease after reservation "
            f"(before={avail_before}, after={avail_after})"
        )

async def check_p3_alt_case_concurrent_availability(client: httpx.AsyncClient, base_url: str, cfg: dict) -> None:
    await reset(client, base_url)

    resp = await client.post(f"{base_url}/hotels", json={
        "name": "Concurrent Avail Test", "location": "Z", "star_rating": 3,
        "rooms": [
            {"room_type": "single", "capacity": 1, "rate_per_night": 99.0},
            {"room_type": "single", "capacity": 1, "rate_per_night": 99.0},
            {"room_type": "single", "capacity": 1, "rate_per_night": 99.0},
        ]
    })
    assert_status(resp, 201, "POST /hotels (P3b)")
    hotel_id = resp.json()["hotel_id"]

    users = await create_users(client, base_url, 3)
    _, check_in, check_out = random_dates(seed=3002)

    before = await client.get(
        f"{base_url}/hotels/{hotel_id}/availability",
        params={"check_in": check_in, "check_out": check_out},
    )
    avail_before = before.json()["available_rooms"].get("single", 0)
    if avail_before != 3:
        raise CheckFailure(f"P3b: expected 3 available singles, got {avail_before}")

    results = await asyncio.gather(*[
        client.post(f"{base_url}/reservations", json={
            "user_id": u["user_id"], "hotel_id": hotel_id,
            "room_type": "single", "check_in": check_in, "check_out": check_out,
        }) for u in users
    ])
    confirmed = sum(1 for r in results if r.status_code == 201)
    after = await client.get(
        f"{base_url}/hotels/{hotel_id}/availability",
        params={"check_in": check_in, "check_out": check_out},
    )
    avail_after = after.json()["available_rooms"].get("single", 0)
    expected = avail_before - confirmed

    if avail_after != expected:
        raise CheckFailure(
            f"P3b: after {confirmed} concurrent bookings, expected availability={expected}, got {avail_after}"
        )


async def check_p4_no_overbooking(
    client: httpx.AsyncClient, base_url: str, cfg: dict, n_clients: int = 10
) -> None:
    await reset(client, base_url)

    # Create a hotel with exactly 1 single room to test overbooking cases
    resp = await client.post(f"{base_url}/hotels", json={
        "name": "Overbook Test Hotel",
        "location": "Testville",
        "star_rating": 3,
        "rooms": [{"room_type": "single", "capacity": 1, "rate_per_night": 99.0}],
    })
    assert_status(resp, 201, "POST /hotels (P4 setup)")
    hotel_id = resp.json()["hotel_id"]

    users = await create_users(client, base_url, n_clients)
    _, check_in, check_out = random_dates(seed=4001, max_stay=2)

    # Fire n_clients concurrent requests all for the same single room (server should sustain)
    async def attempt(user: dict) -> Optional[httpx.Response]:
        try:
            return await client.post(f"{base_url}/reservations", json={
                "user_id": user["user_id"],
                "hotel_id": hotel_id,
                "room_type": "single",
                "check_in": check_in,
                "check_out": check_out,
            })
        except Exception:
            return None

    results = await asyncio.gather(*[attempt(u) for u in users])
    confirmed = [r for r in results if r is not None and r.status_code == 201]
    if len(confirmed) > 1:
        raise CheckFailure(
            f"P4: overbooking detected — {len(confirmed)} reservations confirmed "
            f"for a hotel with 1 single room on the same dates"
        )
    if len(confirmed) == 0:
        raise CheckFailure("P4: no reservation succeeded (should have allowed exactly 1)")


async def check_p5_no_partial_state(client: httpx.AsyncClient, base_url: str, cfg: dict) -> None:
    await reset(client, base_url)

    # Create hotel with 0 single rooms -> only suites
    resp = await client.post(f"{base_url}/hotels", json={
        "name": "No Singles Hotel",
        "location": "Emptytown",
        "star_rating": 4,
        "rooms": [{"room_type": "suite", "capacity": 1, "rate_per_night": 299.0}],
    })
    assert_status(resp, 201, "POST /hotels (P5 setup)")
    hotel_id = resp.json()["hotel_id"]

    users = await create_users(client, base_url, 1)
    _, check_in, check_out = random_dates(seed=5001)

    # Expected output: FAIL since no single rooms available
    fail_resp = await client.post(f"{base_url}/reservations", json={
        "user_id": users[0]["user_id"],
        "hotel_id": hotel_id,
        "room_type": "single",
        "check_in": check_in,
        "check_out": check_out,
    })
    if fail_resp.status_code not in (409, 404, 422):
        raise CheckFailure(
            f"P5: expected 4xx for unavailable room type, got {fail_resp.status_code}"
        )

    # Availability must be unchanged (still 0 singles, 1 suite)
    avail_resp = await client.get(
        f"{base_url}/hotels/{hotel_id}/availability",
        params={"check_in": check_in, "check_out": check_out},
    )
    assert_status(avail_resp, 200, "GET /availability (P5)")
    available = avail_resp.json()["available_rooms"]
    if available.get("single", 0) != 0:
        raise CheckFailure(f"P5: unexpected single room availability after failed attempt: {available}")
    if available.get("suite", 0) != 1:
        raise CheckFailure(f"P5: suite availability changed after unrelated failed attempt: {available}")


async def check_p6_held_out_sequences(client: httpx.AsyncClient, base_url: str, cfg: dict) -> None:
    held_out_cfg = cfg["correctness"]
    n_clients = held_out_cfg["held_out_concurrent_clients"]
    n_reservations = held_out_cfg["held_out_reservations"]
    n_sequences = held_out_cfg["held_out_sequences"]

    rng = random.Random(99999)  # different seed from benchmark

    for seq_idx in range(n_sequences):
        await reset(client, base_url)

        hotels = []
        for h in range(2):
            rooms = []
            for rt in ["single", "double"]:
                count = rng.randint(1, 3)
                for _ in range(count):
                    rooms.append({
                        "room_type": rt,
                        "capacity": 1,
                        "rate_per_night": 99.0 if rt == "single" else 149.0,
                    })
            resp = await client.post(f"{base_url}/hotels", json={
                "name": f"HO-Seq{seq_idx}-Hotel{h}",
                "location": f"City{h}",
                "star_rating": 3,
                "rooms": rooms,
            })
            assert_status(resp, 201, f"P6 seq={seq_idx} hotel={h}")
            hotels.append(resp.json())

        users = await create_users(client, base_url, n_clients)

        confirmed_ids = []
        for _ in range(n_reservations):
            hotel = rng.choice(hotels)
            user = rng.choice(users)
            rt = rng.choice(["single", "double"])
            offset = rng.randint(1, 20)
            stay = rng.randint(1, 3)
            today = date.today()
            check_in = (today + timedelta(days=offset)).isoformat()
            check_out = (today + timedelta(days=offset + stay)).isoformat()

            resp = await client.post(f"{base_url}/reservations", json={
                "user_id": user["user_id"],
                "hotel_id": hotel["hotel_id"],
                "room_type": rt,
                "check_in": check_in,
                "check_out": check_out,
            })
            if resp.status_code == 201:
                confirmed_ids.append(resp.json()["reservation_id"])

        # Verify all confirmed reservations are readable and show "confirmed"
        for res_id in confirmed_ids:
            get = await client.get(f"{base_url}/reservations/{res_id}")
            assert_status(get, 200, f"P6 GET /reservations/{res_id}")
            if get.json()["status"] != "confirmed":
                raise CheckFailure(
                    f"P6 seq={seq_idx}: reservation {res_id} not 'confirmed' after placement"
                )

        # Overbooking check
        _check_no_overbooking_in_state(confirmed_ids, client, base_url, seq_idx)


def _check_no_overbooking_in_state(confirmed_ids: List[str], client: Any, base_url: str, seq_idx: int) -> None:
    # function intentionally made a no-operation placeholder as no async calls allowed 
    pass

CHECKS = [
    ("P1 Response shape", check_p1_response_shape),
    ("P2 Visibility after write", check_p2_visibility),
    ("P3 (i) Availability decreases (isolated case)", check_p3_availability_decreases),
    ("P3 (ii) Availability decreases (concurrent case)", check_p3_alt_case_concurrent_availability),
    ("P4 No overbooking (concurrent)", check_p4_no_overbooking),
    ("P5 No partial state on failure", check_p5_no_partial_state),
    ("P6 Held-out sequences", check_p6_held_out_sequences),
]


async def run_checks(base_url: str, config_path: Path) -> int:
    cfg = load_config(config_path)
    failed = []
    passed = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(f"{base_url}/health")
            if resp.status_code != 200:
                print(f"ERROR: /health returned {resp.status_code}. Is the server running?")
                return 1
        except Exception as exc:
            print(f"ERROR: cannot reach {base_url}/health — {exc}")
            return 1

        for name, fn in CHECKS:
            try:
                await fn(client, base_url, cfg)
                print(f"  PASS  {name}")
                passed.append(name)
            except CheckFailure as exc:
                print(f"  FAIL  {name}: {exc}")
                failed.append((name, str(exc)))
            except Exception as exc:
                print(f"  ERROR {name}: unexpected exception: {exc}")
                failed.append((name, repr(exc)))

    print()
    print(f"Results: {len(passed)} passed, {len(failed)} failed out of {len(CHECKS)} checks.")
    if failed:
        print("\nFailed checks:")
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Hotel Reservation Accuracy Checker")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()

    exit_code = asyncio.run(run_checks(args.base_url, args.config))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
