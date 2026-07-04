Benchmark for moonshine-streaming-medium server.

Two modes, both controlled by `--concurrency`:

**Streaming mode** (`--mode streaming`, default).  Spawns `--concurrency` concurrent WebSocket clients.  Each connects to `--ws-url` (default `ws://localhost:8000/v1/audio/stream`), pushes its audio in `--chunk-s` second chunks at real-time pacing (sleeps to match wall-clock), receives `{"type":"partial","text":...}` events per chunk and a `{"type":"final","text":...}` on `{"type":"finalize"}`.

**Primary metric: TTFT** — wall-clock time from a client pushing a chunk to receiving the corresponding partial transcript, taken across all chunks across all concurrent clients.  Reported as mean / p50 / p90 / p99.  TPOT (latency per subsequent chunk) and audio-seconds-per-wall-second are reported as secondary signals.

**Offline mode** (`--mode offline`).  Spawns `--concurrency` HTTP workers. Each worker loops over the duration: pick an audio sample, fire a `POST /v1/audio/transcriptions` (multipart WAV), wait for the response, fire the next one.  Same TTFT-first reporting; req/s and tok/s are secondary.  Use for vLLM-comparable apples-to-apples sweeps.

Run:
    python benchmark.py --mode streaming --concurrency 32 --chunk-s 2 --duration 30
    python benchmark.py --mode offline   --concurrency 8 --duration 30
