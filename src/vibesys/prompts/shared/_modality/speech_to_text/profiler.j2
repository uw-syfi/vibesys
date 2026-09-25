## Server Type: Speech-to-Text

The server exposes an OpenAI-compatible transcription endpoint:
- `POST /v1/audio/transcriptions` — multipart file upload → JSON/text transcription
- `GET /health` — health check

Key performance axes: **real-time factor** (processing speed vs audio duration), **encoder throughput**, **decoder latency** (autoregressive token loop). The audio encoder forward pass and decoder token loop are the main GPU workloads.

## Profiling Commands

**Warmup** (run after server health check, before and during profiling):
```
python -c "import wave,struct; f=wave.open('/tmp/warmup.wav','w'); f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(b'\\x00\\x00'*16000); f.close()"
curl -s -X POST http://localhost:8077/v1/audio/transcriptions -F "file=@/tmp/warmup.wav" -F "model=whisper" --max-time 300
```

**Workload** (run during profiling when no benchmark tool is available):
```
for i in 1 2 3 4 5; do
  curl -s -X POST http://localhost:8077/v1/audio/transcriptions -F "file=@/tmp/warmup.wav" -F "model=whisper"
done
```
