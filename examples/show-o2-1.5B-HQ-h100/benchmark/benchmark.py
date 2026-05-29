"""HTTP benchmark for a Show-o2 image generation server.

The server contract is intentionally simple and OpenAI-like:

    POST /v1/images/generations
    {"prompt": "...", "num_inference_steps": 20, "guidance_scale": 5.0}

The response must include `data[0].b64_json` containing PNG bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import random
import struct
import time
from pathlib import Path
from statistics import mean

import httpx


PROMPT_POOL = [
    "a small red robot holding a handwritten sign that says VibeServe",
    "a studio product photo of a ceramic mug shaped like a rocket",
    "a watercolor landscape with a lighthouse at sunrise",
    "a close-up macro photo of a translucent blue mechanical keyboard switch",
    "a clean app icon for an AI inference server, white background",
]


def select_prompt(args: argparse.Namespace, rng: random.Random, idx: int, *, warmup: bool = False) -> str:
    if args.prompt is not None:
        return args.prompt
    if warmup:
        return PROMPT_POOL[idx % len(PROMPT_POOL)]
    return rng.choice(PROMPT_POOL)


def request_seed_for(args: argparse.Namespace, idx: int) -> int | None:
    if args.request_seed is None:
        return None
    if args.fixed_request_seed:
        return args.request_seed
    return args.request_seed + idx


def image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and image_bytes[12:16] == b"IHDR":
        width, height = struct.unpack(">II", image_bytes[16:24])
        return int(width), int(height)
    if image_bytes.startswith(b"P6"):
        header_parts = image_bytes.split(None, 4)
        if len(header_parts) >= 4 and header_parts[0] == b"P6" and header_parts[3] == b"255":
            return int(header_parts[1]), int(header_parts[2])
    raise ValueError("response image is not a PNG or PPM")


async def send_request(
    client: httpx.AsyncClient,
    url: str,
    prompt: str,
    steps: int,
    guidance_scale: float,
    seed: int | None,
    timeout: float,
    collect_server_timings: bool,
    postprocess_mode: str | None,
    response_format: str,
    image_save_path: Path | None = None,
) -> dict:
    body = {
        "prompt": prompt,
        "num_inference_steps": steps,
        "guidance_scale": guidance_scale,
    }
    if seed is not None:
        body["seed"] = seed
    if collect_server_timings:
        body["include_timings"] = True
    if postprocess_mode is not None:
        body["postprocess_mode"] = postprocess_mode
    if response_format != "b64_json":
        body["response_format"] = response_format

    started = time.perf_counter()
    try:
        resp = await client.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        if response_format in {"png", "ppm"}:
            image_bytes = resp.content
            timings_header = resp.headers.get("X-ShowO2-Timings-Ms", "{}")
            server_timings = json.loads(timings_header) if collect_server_timings else {}
        else:
            payload = resp.json()
            b64 = payload["data"][0]["b64_json"]
            image_bytes = base64.b64decode(b64)
            server_timings = payload.get("timings_ms", {}) if collect_server_timings else {}
        width, height = image_dimensions(image_bytes)
        if image_save_path is not None:
            image_save_path.parent.mkdir(parents=True, exist_ok=True)
            image_save_path.write_bytes(image_bytes)
        return {
            "error": None,
            "latency": time.perf_counter() - started,
            "image_bytes": len(image_bytes),
            "image_width": width,
            "image_height": height,
            "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "image_path": str(image_save_path) if image_save_path is not None else None,
            "server_timings": server_timings,
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "latency": time.perf_counter() - started,
            "image_bytes": 0,
            "image_width": 0,
            "image_height": 0,
            "image_sha256": None,
            "image_path": None,
            "server_timings": {},
        }


def image_save_path(args: argparse.Namespace, phase: str, idx: int) -> Path | None:
    if args.save_images_dir is None:
        return None
    suffix = "ppm" if args.response_format == "ppm" else "png"
    return Path(args.save_images_dir) / f"{phase}_{idx:04d}.{suffix}"


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def stats(values: list[float], multiplier: float = 1.0) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "mean": mean(ordered) * multiplier,
        "p50": percentile(ordered, 50) * multiplier,
        "p90": percentile(ordered, 90) * multiplier,
        "p95": percentile(ordered, 95) * multiplier,
        "p99": percentile(ordered, 99) * multiplier,
    }


def server_timing_stats(results: list[dict]) -> dict[str, dict]:
    successes = [r for r in results if r["error"] is None]
    timing_keys = sorted(
        {
            key
            for result in successes
            for key, value in result.get("server_timings", {}).items()
            if isinstance(value, (int, float))
        }
    )
    return {
        key: stats(
            [
                float(result["server_timings"][key])
                for result in successes
                if isinstance(result.get("server_timings", {}).get(key), (int, float))
            ]
        )
        for key in timing_keys
    }


def summarize_results(results: list[dict], wall_clock: float) -> dict:
    successes = [r for r in results if r["error"] is None]
    errors = [r for r in results if r["error"] is not None]
    latencies = [r["latency"] for r in successes]
    image_sizes = [r["image_bytes"] for r in successes]
    image_widths = [r["image_width"] for r in successes]
    image_heights = [r["image_height"] for r in successes]
    image_hashes = [r["image_sha256"] for r in successes if r.get("image_sha256")]
    image_paths = [r["image_path"] for r in successes if r.get("image_path")]
    return {
        "num_requests": len(results),
        "num_completed": len(successes),
        "num_failed": len(errors),
        "actual_duration": wall_clock,
        "request_throughput": len(successes) / wall_clock if wall_clock > 0 else 0,
        "latency": stats(latencies, multiplier=1000.0),
        "image_bytes": stats(image_sizes),
        "image_width": stats(image_widths),
        "image_height": stats(image_heights),
        "image_sha256": image_hashes,
        "unique_image_sha256": sorted(set(image_hashes)),
        "image_paths": image_paths,
        "server_timings": server_timing_stats(results),
    }


async def run_warmup(client: httpx.AsyncClient, args: argparse.Namespace, url: str) -> tuple[list[dict], float]:
    if args.warmup_requests <= 0:
        return [], 0.0

    results = []
    warmup_start = time.perf_counter()
    for idx in range(args.warmup_requests):
        prompt = select_prompt(args, random.Random(args.seed), idx, warmup=True)
        request_seed = request_seed_for(args, idx)
        results.append(
            await send_request(
                client,
                url,
                prompt,
                args.steps,
                args.guidance_scale,
                request_seed,
                args.timeout,
                args.collect_server_timings,
                args.postprocess_mode,
                args.response_format,
                image_save_path(args, "warmup", idx),
            )
        )
    return results, time.perf_counter() - warmup_start


async def run_closed_loop(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    url: str,
    rng: random.Random,
    total_requests: int,
    use_duration: bool,
    bench_start: float,
) -> list[dict]:
    results = []
    sent = 0
    while sent < total_requests:
        if use_duration and (time.perf_counter() - bench_start) >= args.duration:
            break
        prompt = select_prompt(args, rng, sent)
        request_seed = request_seed_for(args, sent)
        results.append(
            await send_request(
                client,
                url,
                prompt,
                args.steps,
                args.guidance_scale,
                request_seed,
                args.timeout,
                args.collect_server_timings,
                args.postprocess_mode,
                args.response_format,
                image_save_path(args, "request", sent),
            )
        )
        sent += 1
    return results


async def run_open_loop(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    url: str,
    rng: random.Random,
    total_requests: int,
    use_duration: bool,
    bench_start: float,
) -> list[dict]:
    tasks: list[asyncio.Task] = []
    sent = 0
    while sent < total_requests:
        if use_duration and (time.perf_counter() - bench_start) >= args.duration:
            break
        prompt = select_prompt(args, rng, sent)
        request_seed = request_seed_for(args, sent)
        tasks.append(
            asyncio.create_task(
                send_request(
                    client,
                    url,
                    prompt,
                    args.steps,
                    args.guidance_scale,
                    request_seed,
                    args.timeout,
                    args.collect_server_timings,
                    args.postprocess_mode,
                    args.response_format,
                    image_save_path(args, "request", sent),
                )
            )
        )
        sent += 1
        if sent < total_requests:
            delay = -math.log(1.0 - rng.random()) / args.rate
            if use_duration:
                remaining = args.duration - (time.perf_counter() - bench_start)
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            await asyncio.sleep(delay)
    return await asyncio.gather(*tasks)


async def run_benchmark(args: argparse.Namespace) -> dict:
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint
    total_requests = args.num_requests if args.num_requests is not None else 10**9
    use_duration = args.num_requests is None

    async with httpx.AsyncClient() as client:
        warmup_results, warmup_duration = await run_warmup(client, args, url)
        bench_start = time.perf_counter()
        if args.closed_loop:
            results = await run_closed_loop(
                client,
                args,
                url,
                rng,
                total_requests,
                use_duration,
                bench_start,
            )
        else:
            results = await run_open_loop(
                client,
                args,
                url,
                rng,
                total_requests,
                use_duration,
                bench_start,
            )
        bench_end = time.perf_counter()

    wall_clock = bench_end - bench_start
    successes = [r for r in results if r["error"] is None]
    errors = [r for r in results if r["error"] is not None]
    latencies = [r["latency"] for r in successes]
    image_sizes = [r["image_bytes"] for r in successes]
    measured_summary = summarize_results(results, wall_clock)

    print()
    print("=" * 40)
    print("  Show-o2 Benchmark Results")
    print("=" * 40)
    print(f"Endpoint:          {url}")
    print(f"Mode:              {'closed-loop' if args.closed_loop else 'open-loop'}")
    if warmup_results:
        warmup_summary = summarize_results(warmup_results, warmup_duration)
        print(
            "Warmup:           "
            f"{warmup_summary['num_completed']}/{warmup_summary['num_requests']} requests "
            f"({warmup_summary['num_failed']} errors, {warmup_duration:.2f}s)"
        )
    print(f"Duration:          {wall_clock:.2f}s")
    print(f"Completed:         {len(successes)}/{len(results)} requests ({len(errors)} errors)")
    print(f"Request throughput:{len(successes) / wall_clock if wall_clock > 0 else 0:.2f} req/s")
    if latencies:
        latency_ms = stats(latencies, multiplier=1000.0)
        print(f"Latency mean:      {latency_ms['mean']:.1f} ms")
        print(f"Latency p90:       {latency_ms['p90']:.1f} ms")
    if measured_summary["server_timings"]:
        total_timing = measured_summary["server_timings"].get("total_ms")
        if total_timing:
            print(f"Server total mean: {total_timing['mean']:.1f} ms")
    if errors:
        print("Errors:")
        for idx, err in enumerate(errors[:5]):
            print(f"  [{idx}] {err['error'][:160]}")

    result = {
        "config": {
            "url": url,
            "rate": args.rate,
            "duration": args.duration,
            "steps": args.steps,
            "guidance_scale": args.guidance_scale,
            "prompt": args.prompt,
            "seed": args.seed,
            "request_seed": args.request_seed,
            "fixed_request_seed": args.fixed_request_seed,
            "warmup_requests": args.warmup_requests,
            "closed_loop": args.closed_loop,
            "collect_server_timings": args.collect_server_timings,
            "postprocess_mode": args.postprocess_mode,
            "response_format": args.response_format,
            "save_images_dir": args.save_images_dir,
        },
        "warmup": summarize_results(warmup_results, warmup_duration),
        "num_requests": len(results),
        "num_completed": len(successes),
        "num_failed": len(errors),
        "actual_duration": wall_clock,
        "request_throughput": len(successes) / wall_clock if wall_clock > 0 else 0,
        "latency": stats(latencies, multiplier=1000.0),
        "image_bytes": stats(image_sizes),
        "image_width": measured_summary["image_width"],
        "image_height": measured_summary["image_height"],
        "image_sha256": measured_summary["image_sha256"],
        "unique_image_sha256": measured_summary["unique_image_sha256"],
        "image_paths": measured_summary["image_paths"],
        "server_timings": measured_summary["server_timings"],
    }

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Results written to {args.output_json}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark a Show-o2 HTTP image server.")
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/images/generations", help="Endpoint path")
    parser.add_argument("--rate", type=float, default=1.0, help="Request rate in req/s")
    parser.add_argument("--duration", type=float, default=60.0, help="Duration when num requests is unset")
    parser.add_argument("--num-requests", type=int, default=None, help="Total requests")
    parser.add_argument(
        "--closed-loop",
        action="store_true",
        help="Send each measured request after the previous one finishes instead of open-loop rate scheduling",
    )
    parser.add_argument(
        "--collect-server-timings",
        action="store_true",
        help="Ask the server to include per-request timings_ms and aggregate them",
    )
    parser.add_argument(
        "--postprocess-mode",
        choices=["upstream", "cpu", "native"],
        default=None,
        help="Optional server postprocess mode override for image requests",
    )
    parser.add_argument(
        "--response-format",
        default="b64_json",
        choices=["b64_json", "png", "ppm"],
        help="Response format to request from the server.",
    )
    parser.add_argument(
        "--warmup-requests",
        type=int,
        default=0,
        help="Requests to run before measurement and exclude from latency/throughput stats",
    )
    parser.add_argument("--steps", type=int, default=20, help="Diffusion inference steps per request")
    parser.add_argument("--guidance-scale", type=float, default=5.0, help="Classifier-free guidance scale")
    parser.add_argument("--prompt", type=str, default=None, help="Fixed prompt for every benchmark request")
    parser.add_argument("--seed", type=int, default=42, help="Benchmark scheduling seed")
    parser.add_argument("--request-seed", type=int, default=None, help="Base model seed for deterministic requests")
    parser.add_argument(
        "--fixed-request-seed",
        action="store_true",
        help="Use request seed unchanged for every request instead of incrementing it by request index",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="Per-request timeout in seconds")
    parser.add_argument("--output-json", type=str, default=None, help="Write structured result JSON")
    parser.add_argument(
        "--save-images-dir",
        type=str,
        default=None,
        help="Save completed warmup and measured response PNGs into this directory",
    )
    args = parser.parse_args()
    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
