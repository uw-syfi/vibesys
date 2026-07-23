"""Offline serving benchmark for whisper-large-v3.

Drives the candidate's OpenAI-compatible `/v1/audio/transcriptions` endpoint with
`--concurrency` concurrent clients replaying the test-audio pool, and reports
offline throughput (the headline metric) plus latency percentiles.

Headline metric (what VibeServe scores): `requests_per_second`. Secondary,
printed for humans: audio-seconds transcribed per wall-second, and end-to-end
latency mean / p50 / p95 / p99.

The candidate server must already be running (the objective is to serve, not to
launch); point `--url` at it.

Usage:
    uv run python benchmark/benchmark.py --url http://localhost:8000 \
        --concurrency 8 --num-requests 64 [--output-json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError("httpx is required: pip install httpx") from exc


def load_audio_pool(audio_dir: Path):
    """Return [(wav_bytes, duration_s, filename)]."""
    manifest = {}
    manifest_path = audio_dir / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            for entry in json.load(f):
                manifest[entry["file"]] = entry
    pool = []
    for p in sorted(audio_dir.glob("*.wav")):
        with wave.open(str(p), "rb") as wf:
            assert wf.getsampwidth() == 2, f"{p} is not 16-bit PCM"
            assert wf.getnchannels() == 1, f"{p} is not mono"
            assert wf.getframerate() == 16000, f"{p} is not 16 kHz"
            dur = wf.getnframes() / wf.getframerate()
        meta = manifest.get(p.name, {})
        pool.append((p.read_bytes(), meta.get("duration_s", dur), p.name))
    if not pool:
        raise FileNotFoundError(f"No audio files in {audio_dir}")
    return pool


async def _one_request(client, url, wav_bytes, filename):
    files = {"file": (filename, wav_bytes, "audio/wav")}
    data = {"model": "whisper-large-v3", "response_format": "json"}
    t0 = time.perf_counter()
    try:
        resp = await client.post(f"{url}/v1/audio/transcriptions", files=files, data=data)
        resp.raise_for_status()
        return {"latency_s": time.perf_counter() - t0, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"latency_s": time.perf_counter() - t0, "error": str(exc)}


async def run_offline(args, pool):
    sem = asyncio.Semaphore(args.concurrency)
    results = []
    audio_seconds = 0.0

    async def worker(i):
        nonlocal audio_seconds
        wav_bytes, dur, filename = pool[i % len(pool)]
        async with sem:
            r = await _one_request(client, args.url, wav_bytes, filename)
        if r["error"] is None:
            audio_seconds += dur
        results.append(r)

    timeout = httpx.Timeout(args.request_timeout)
    limits = httpx.Limits(max_connections=max(args.concurrency + 4, 16))
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        # warmup (not scored)
        await asyncio.gather(*[worker(i) for i in range(min(args.concurrency, len(pool)))])
        results.clear()
        audio_seconds = 0.0

        t_start = time.perf_counter()
        await asyncio.gather(*[worker(i) for i in range(args.num_requests)])
        wall = time.perf_counter() - t_start

    succ = [r for r in results if r["error"] is None]
    lats = sorted(r["latency_s"] for r in succ)
    rps = len(succ) / wall if wall > 0 else 0.0

    def pct(p):
        if not lats:
            return 0.0
        k = min(len(lats) - 1, int(round((p / 100) * (len(lats) - 1))))
        return lats[k]

    print("=" * 44)
    print("  whisper-large-v3 offline benchmark")
    print("=" * 44)
    print(f"URL:            {args.url}")
    print(f"Concurrency:    {args.concurrency}")
    print(f"Completed:      {len(succ)}/{len(results)} ({len(results) - len(succ)} errors)")
    print(f"Wall time:      {wall:.2f}s")
    print(f"Audio pool:     {sum(p[1] for p in pool):.1f}s over {len(pool)} clips")
    print()
    print(f"Throughput:     {rps:.2f} req/s")
    print(f"                {audio_seconds / wall:.1f} audio-s / wall-s")
    if lats:
        mean = sum(lats) / len(lats)
        print(
            f"Latency (s):    mean {mean:.3f}  p50 {pct(50):.3f}  "
            f"p95 {pct(95):.3f}  p99 {pct(99):.3f}"
        )
    print("=" * 44)

    if args.num_requests and len(succ) < args.num_requests:
        # Surface a silent-truncation / error condition rather than reporting a
        # rosy throughput off a handful of completed requests.
        print(f"WARNING: only {len(succ)}/{args.num_requests} requests succeeded.")

    return {"requests_per_second": rps}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", type=str, default="http://localhost:8000")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--num-requests", type=int, default=64)
    p.add_argument("--request-timeout", type=float, default=120.0)
    p.add_argument("--audio-dir", type=str, default=None)
    p.add_argument("--output-json", type=str, default=None)
    args = p.parse_args()

    audio_dir = (
        Path(args.audio_dir).resolve()
        if args.audio_dir
        else (Path(__file__).parent.parent / "test_audio").resolve()
    )
    pool = load_audio_pool(audio_dir)

    result = asyncio.run(run_offline(args, pool))

    if args.output_json:
        # Trusted single-scalar metric (see vibeserve.input.toml [benchmark.result]).
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResults written to {args.output_json}")


if __name__ == "__main__":
    main()
