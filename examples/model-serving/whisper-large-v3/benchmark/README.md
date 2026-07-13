# Benchmark — whisper-large-v3 (offline)

`benchmark.py` drives the candidate's `/v1/audio/transcriptions` endpoint with
`--concurrency` concurrent clients over the `test_audio/` pool and reports the
headline metric `requests_per_second` (declared in the manifest's
`[benchmark.result]`), plus audio-s/wall-s and latency percentiles for humans.

The candidate server must already be running.

```bash
uv run python benchmark/benchmark.py --url http://localhost:8000 \
    --concurrency 8 --num-requests 64 --output-json out.json
```
