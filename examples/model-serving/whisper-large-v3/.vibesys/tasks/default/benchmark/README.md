# Benchmark: whisper-large-v3 (offline)

`requests.jsonl` is a checked-in Request Factory
`multimodal-independent-v1` trace. Its 64 requests cycle deterministically over
the four hashed WAV fixtures. The pinned evaluator engine sends them to the
candidate's `/v1/audio/transcriptions` endpoint under saturated load with at
most eight active requests.

The candidate server must already be running. The manifest invokes
`request-factory-engine` directly. VibeSys appends `--summary-path` and reads
`request_throughput_per_s` from the Request Factory summary as the trusted
headline metric. Per-request JSONL and Parquet timelines are disabled.

For a standalone run from the task root:

```bash
session_runner \
  --trace benchmark/requests.jsonl \
  --input-file-format multimodal-independent-v1 \
  --base-url http://localhost:8000/v1 \
  --model whisper-large-v3 \
  --backend openai-transcriptions \
  --dialect openai \
  --temperature 0 \
  --arrival-mode saturated \
  --max-concurrency 8 \
  --request-log false \
  --timeline false \
  --summary-path out.json
```

There is no separate warmup phase. Cold model, allocator, compilation, and
connection setup costs observed by the first requests are part of the measured
run. Request throughput uses Request Factory's first-submit-to-last-complete
window. Request Factory currently applies a fixed 3600-second timeout to this
nonstreaming endpoint.

The pinned Cargo tool is currently supported only by the Local run environment.
Docker, Modal, and SkyPilot reject tool-backed evaluator packages during
provisioning.
