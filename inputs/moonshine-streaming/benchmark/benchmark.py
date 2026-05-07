"""
Serving benchmark for moonshine-streaming-medium.

Two modes:

* `streaming`: N concurrent WebSocket clients, each pushing 16 kHz PCM
  audio in `--chunk-s` second chunks at *real-time* pacing.  Measures
  TTFT (send→first partial), TPOT (per chunk after first), and
  audio-seconds-per-wall-second.
* `offline`: Poisson-arrival HTTP `/v1/audio/transcriptions` clients.
  Same shape as the whisper-large benchmark; used for vLLM-comparable
  numbers.

Audio is loaded from `test_audio/` (or `--audio-dir`).

Usage:
    python benchmark.py --mode streaming --concurrency 4 --chunk-s 2 --duration 30
    python benchmark.py --mode offline   --rate 2 --duration 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import struct
import time
import wave
from pathlib import Path

try:
    import httpx
except ImportError:
    raise ImportError("httpx is required: pip install httpx")
try:
    import websockets
except ImportError:
    websockets = None  # only required for streaming mode


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------


def load_audio_pool(audio_dir: Path):
    """Load WAV files from a directory.  Returns
    list of (wav_bytes, pcm16_bytes, sample_rate, duration_s, filename, ref_text)."""
    manifest_path = audio_dir / "manifest.json"
    pool = []
    manifest_entries = {}
    if manifest_path.exists():
        with open(manifest_path) as f:
            for entry in json.load(f):
                manifest_entries[entry["file"]] = entry

    for p in sorted(audio_dir.glob("*.wav")):
        with wave.open(str(p), "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            assert sw == 2, f"{p} is not 16-bit PCM"
            assert ch == 1, f"{p} is not mono"
            assert sr == 16000, f"{p} is not 16 kHz"
            pcm = wf.readframes(n)
        wav_bytes = p.read_bytes()
        meta = manifest_entries.get(p.name, {})
        pool.append((
            wav_bytes,
            pcm,
            sr,
            meta.get("duration_s", n / sr),
            p.name,
            meta.get("text", ""),
        ))
    if not pool:
        raise FileNotFoundError(f"No audio files in {audio_dir}")
    return pool


# ---------------------------------------------------------------------------
# Streaming-mode client
# ---------------------------------------------------------------------------


async def streaming_client(
    name: str,
    url: str,
    pcm: bytes,
    sample_rate: int,
    duration_s: float,
    chunk_s: float,
    log: list[dict],
) -> dict:
    """Pushes `pcm` over WebSocket in `chunk_s` chunks at real-time pacing."""
    if websockets is None:
        raise RuntimeError("websockets package required for streaming mode")
    samples_per_chunk = int(chunk_s * sample_rate)
    bytes_per_chunk = samples_per_chunk * 2
    total_chunks = math.ceil(len(pcm) / bytes_per_chunk)

    sent_times: list[float] = []
    partial_times: list[float] = []
    finalized_at: float | None = None
    final_text = ""
    error: str | None = None

    t_start = time.perf_counter()
    try:
        async with websockets.connect(url, max_size=None) as ws:
            async def reader():
                nonlocal finalized_at, final_text
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        continue
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    now = time.perf_counter()
                    if data.get("type") == "partial":
                        partial_times.append(now)
                    elif data.get("type") == "final":
                        finalized_at = now
                        final_text = data.get("text", "")
                        return
            rd = asyncio.create_task(reader())

            for i in range(total_chunks):
                target = t_start + (i + 1) * chunk_s
                now = time.perf_counter()
                if target > now:
                    await asyncio.sleep(target - now)
                lo = i * bytes_per_chunk
                hi = min(lo + bytes_per_chunk, len(pcm))
                await ws.send(pcm[lo:hi])
                sent_times.append(time.perf_counter())
            await ws.send(json.dumps({"type": "finalize"}))
            await asyncio.wait_for(rd, timeout=30.0)
    except Exception as exc:
        error = str(exc)
    t_end = time.perf_counter()

    # latencies
    chunk_latencies: list[float] = []
    for i, ts in enumerate(sent_times):
        future = [pt for pt in partial_times if pt >= ts]
        if future:
            chunk_latencies.append(future[0] - ts)
    ttft = chunk_latencies[0] if chunk_latencies else None
    tpot = (sum(chunk_latencies[1:]) / max(1, len(chunk_latencies) - 1)
            if len(chunk_latencies) > 1 else None)
    return {
        "name": name,
        "error": error,
        "duration_s": duration_s,
        "wall_s": t_end - t_start,
        "n_sent": len(sent_times),
        "n_partials": len(partial_times),
        "ttft": ttft,
        "tpot": tpot,
        "final_text": final_text,
    }


async def run_streaming(args, audio_pool):
    rng = random.Random(args.seed)
    url = args.ws_url
    print(f"Streaming benchmark: concurrency={args.concurrency} chunk_s={args.chunk_s}s "
          f"duration_budget={args.duration}s url={url}")

    log: list[dict] = []
    tasks: list[asyncio.Task] = []
    for i in range(args.concurrency):
        wav_bytes, pcm, sr, dur, fname, _ = rng.choice(audio_pool)
        # Cap audio per client by --duration if it'd exceed.
        if args.duration and dur > args.duration:
            n_keep = int(args.duration * sr) * 2
            pcm = pcm[:n_keep]
            dur = args.duration
        tasks.append(asyncio.create_task(streaming_client(
            f"C{i}", url, pcm, sr, dur, args.chunk_s, log)))
    t0 = time.perf_counter()
    results = await asyncio.gather(*tasks)
    wall = time.perf_counter() - t0

    succ = [r for r in results if r["error"] is None]
    fail = [r for r in results if r["error"] is not None]
    audio_total = sum(r["duration_s"] for r in succ)
    aud_per_s = audio_total / wall if wall else 0.0
    ttfts = [r["ttft"] for r in succ if r["ttft"] is not None]
    tpots = [r["tpot"] for r in succ if r["tpot"] is not None]

    print()
    print("=" * 40)
    print("  Streaming Benchmark Results")
    print("=" * 40)
    print(f"Clients:           {len(succ)}/{len(results)} ({len(fail)} errors)")
    print(f"Wall time:         {wall:.2f}s")
    print(f"Audio processed:   {audio_total:.1f}s")
    print()
    print("TTFT (send→first partial)  ← primary metric:")
    print(_format_stats(ttfts))
    sorted_ttfts = sorted(ttfts) if ttfts else []
    if sorted_ttfts:
        print(f"Primary metric: ttft_p50_ms={_percentile(sorted_ttfts, 50)*1000:.1f}")
    print()
    print("Secondary:")
    print(f"  audio_s_per_s:   {aud_per_s:.2f}")
    print()
    print("TPOT (per chunk after first):")
    print(_format_stats(tpots))
    if fail:
        print("Errors:")
        for r in fail[:5]:
            print(f"  - {r['error'][:100]}")

    return {
        "mode": "streaming",
        "concurrency": args.concurrency,
        "chunk_s": args.chunk_s,
        "wall_s": wall,
        "audio_s": audio_total,
        "audio_s_per_s": aud_per_s,
        "ttft": _pct_block(sorted(ttfts)),
        "tpot": _pct_block(sorted(tpots)),
        "num_completed": len(succ),
        "num_failed": len(fail),
    }


# ---------------------------------------------------------------------------
# Offline-mode client (HTTP)
# ---------------------------------------------------------------------------


async def offline_request(client: httpx.AsyncClient, url: str, wav_bytes: bytes, stream: bool):
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    data = {"model": "moonshine"}
    if stream:
        data["stream"] = "true"

    t_send = time.perf_counter()
    t_first = None
    t_done = None
    n_tokens = 0
    text = ""
    err = None
    try:
        if stream:
            async with client.stream("POST", url, files=files, data=data, timeout=120.0) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload.strip() == "[DONE]":
                        t_done = time.perf_counter()
                        break
                    chunk = json.loads(payload)
                    delta = chunk.get("delta", chunk.get("text", ""))
                    if delta:
                        n_tokens += 1
                        text += delta
                        if t_first is None:
                            t_first = time.perf_counter()
        else:
            resp = await client.post(url, files=files, data=data, timeout=120.0)
            resp.raise_for_status()
            j = resp.json()
            t_done = time.perf_counter()
            text = j.get("text", "")
            n_tokens = max(1, len(text.split())) if text else 0
            t_first = t_done
    except Exception as exc:
        err = str(exc)
        t_done = time.perf_counter()

    if t_done is None:
        t_done = time.perf_counter()
    res = {
        "error": err,
        "n_tokens": n_tokens,
        "total_latency": t_done - t_send,
        "text_len": len(text),
    }
    if t_first is not None:
        res["ttft"] = t_first - t_send
        res["tpot"] = (t_done - t_first) / (n_tokens - 1) if n_tokens > 1 else None
    else:
        res["ttft"] = None
        res["tpot"] = None
    return res


async def run_offline(args, audio_pool):
    """Offline mode: keep `--concurrency` requests in flight for `--duration`
    seconds.  Each worker picks an audio sample, fires the request, and
    immediately starts the next one when it returns — i.e. measures the
    server's saturation throughput at the chosen concurrency level."""
    rng = random.Random(args.seed)
    url = args.url.rstrip("/") + args.endpoint
    print(f"Offline benchmark: concurrency={args.concurrency} duration={args.duration}s url={url}")

    stop_at: float | None = None
    results: list[dict] = []

    async def worker(wid: int, client: httpx.AsyncClient):
        local_rng = random.Random(args.seed + wid)
        while time.perf_counter() < stop_at:
            wav_bytes, _, _, _, _, _ = local_rng.choice(audio_pool)
            r = await offline_request(client, url, wav_bytes, args.stream)
            results.append(r)
            if args.num_requests is not None and len(results) >= args.num_requests:
                return

    async with httpx.AsyncClient() as client:
        t0 = time.perf_counter()
        stop_at = t0 + args.duration
        await asyncio.gather(*(worker(i, client) for i in range(args.concurrency)))
        wall = time.perf_counter() - t0

    succ = [r for r in results if r["error"] is None]
    fail = [r for r in results if r["error"] is not None]
    ttfts = [r["ttft"] for r in succ if r["ttft"] is not None]
    tpots = [r["tpot"] for r in succ if r["tpot"] is not None]
    lats = [r["total_latency"] for r in succ]
    n_tokens = sum(r["n_tokens"] for r in succ)

    print()
    print("=" * 40)
    print("  Offline Benchmark Results")
    print("=" * 40)
    print(f"Backend URL:       {url}")
    print(f"Concurrency:       {args.concurrency}")
    print(f"Duration:          {wall:.1f}s")
    print(f"Completed:         {len(succ)}/{len(results)} ({len(fail)} errors)")
    print(f"Streaming SSE:     {args.stream}")
    print()
    print(f"Throughput: {len(succ)/wall:.2f} req/s, {n_tokens/wall:.1f} tok/s")
    print()
    print("TTFT:")
    print(_format_stats(ttfts))
    print("TPOT:")
    print(_format_stats(tpots))
    print("Total latency:")
    print(_format_stats(lats))
    return {
        "mode": "offline",
        "concurrency": args.concurrency,
        "wall_s": wall,
        "num_completed": len(succ),
        "num_failed": len(fail),
        "request_throughput": len(succ) / wall if wall else 0.0,
        "token_throughput": n_tokens / wall if wall else 0.0,
        "ttft": _pct_block(sorted(ttfts)),
        "tpot": _pct_block(sorted(tpots)),
        "total_latency": _pct_block(sorted(lats)),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _percentile(s, p):
    if not s:
        return float("nan")
    k = (len(s) - 1) * p / 100
    f = math.floor(k); c = math.ceil(k)
    return s[int(k)] if f == c else s[f] * (c - k) + s[c] * (k - f)


def _format_stats(values):
    if not values:
        return "    (no data)\n"
    s = sorted(values)
    return (
        f"  Mean:    {sum(s)/len(s)*1000:.1f} ms\n"
        f"  Median:  {_percentile(s, 50)*1000:.1f} ms\n"
        f"  P90:     {_percentile(s, 90)*1000:.1f} ms\n"
        f"  P99:     {_percentile(s, 99)*1000:.1f} ms\n"
    )


def _pct_block(s):
    if not s:
        return None
    return {
        "mean_ms": sum(s) / len(s) * 1000,
        "p50_ms": _percentile(s, 50) * 1000,
        "p90_ms": _percentile(s, 90) * 1000,
        "p95_ms": _percentile(s, 95) * 1000,
        "p99_ms": _percentile(s, 99) * 1000,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["streaming", "offline"], default="streaming")
    # streaming
    p.add_argument("--ws-url", default="ws://localhost:8000/v1/audio/stream",
                   help="WebSocket URL (streaming mode)")
    p.add_argument("--chunk-s", type=float, default=2.0, help="Audio chunk seconds")
    # offline
    p.add_argument("--url", default="http://localhost:8000",
                   help="Server base URL (offline mode)")
    p.add_argument("--endpoint", default="/v1/audio/transcriptions")
    p.add_argument("--num-requests", type=int, default=None,
                   help="Optional cap on total requests (offline mode)")
    p.add_argument("--stream", action="store_true", help="SSE streaming for offline mode")
    # shared
    p.add_argument("--concurrency", type=int, default=32,
                   help="Concurrent in-flight requests (streaming: number of "
                        "live clients; offline: number of workers each looping "
                        "request→request for the duration)")
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--audio-dir", type=str, default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output-json", type=str, default=None)
    args = p.parse_args()

    if args.audio_dir:
        audio_dir = Path(args.audio_dir).resolve()
    else:
        audio_dir = (Path(__file__).parent / "test_audio").resolve()
        if not audio_dir.is_dir():
            audio_dir = (Path(__file__).parent.parent / "test_audio").resolve()

    pool = load_audio_pool(audio_dir)
    print(f"Audio pool: {len(pool)} clips, total {sum(p[3] for p in pool):.1f}s")

    if args.mode == "streaming":
        result = asyncio.run(run_streaming(args, pool))
    else:
        result = asyncio.run(run_offline(args, pool))

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResults written to {args.output_json}")


if __name__ == "__main__":
    main()
