## Server Type: Text-to-Speech

The server exposes an OpenAI-compatible TTS endpoint:
- `POST /v1/audio/speech` — `{model, input, voice, response_format, speed}` → streaming audio bytes
- `GET /health` — health check

Key performance axes: **time-to-first-audio-chunk**, **real-time factor** (generation speed vs playback speed), **vocoder throughput**. The acoustic model and vocoder are the main GPU workloads.

## Profiling Commands

**Warmup** (run after server health check, before and during profiling):
```
curl -s -X POST http://localhost:8077/v1/audio/speech -H "Content-Type: application/json" -d '{"model":"tts","input":"warmup","voice":"alloy"}' --max-time 300 -o /dev/null
```

**Workload** (run during profiling when no benchmark tool is available):
```
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8077/v1/audio/speech -H "Content-Type: application/json" \
    -d '{"model":"tts","input":"The quick brown fox jumps over the lazy dog.","voice":"alloy"}' -o /dev/null
done
```
